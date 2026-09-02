"""Scorecard computation for design screen 1g.

Business rules (targets, weights, thresholds) come from settings.KPI_SETTINGS
so they can be tuned without a code change — see the assumptions the design
called out: 48h request→PO, 2h to "in process", 8h to invoice, ≤2% error
rate, weighted score × base bonus, only assistant-attributable errors count
against pay.

The "on time?" checks themselves live on the models (PurchaseRequest.is_po_on_time,
PickingList.is_in_process_on_time / is_invoice_on_time) — this module reuses
those rather than re-deriving the same hour math, so the rule only exists in
one place.
"""

import calendar
from dataclasses import dataclass, field
from datetime import datetime

from django.conf import settings
from django.utils import timezone

from .models import BillingError, PickingList, PurchaseRequest


@dataclass
class IndicatorScore:
    label: str
    target_label: str
    actual_label: str
    weight: float  # 0..100, the indicator's share of the 100-point score
    score: float  # 0..weight, i.e. points actually earned out of that share


@dataclass
class Scorecard:
    year: int
    month: int
    avg_po_hours: float | None
    po_on_time_count: int
    po_total_count: int
    avg_invoice_hours: float | None
    invoice_lists_count: int
    error_rate: float | None
    error_count: int
    attainment: float  # 0..100
    bonus_payable: float
    bonus_threshold_pct: float = 70.0  # gauge tick — below this, no bonus
    attainment_target_pct: float = 90.0  # gauge tick — aspirational mark
    indicators: list[IndicatorScore] = field(default_factory=list)
    errors: list[BillingError] = field(default_factory=list)

    @property
    def month_label(self):
        from django.utils.dates import MONTHS
        return f"{str(MONTHS[self.month]).capitalize()} {self.year}"

    @property
    def po_on_time_rate(self):
        if not self.po_total_count:
            return None
        return self.po_on_time_count / self.po_total_count


def month_bounds(year: int, month: int):
    start = timezone.make_aware(datetime(year, month, 1))
    last_day = calendar.monthrange(year, month)[1]
    end = timezone.make_aware(datetime(year, month, last_day, 23, 59, 59))
    return start, end


def _on_time_rate(flags: list[bool]) -> float | None:
    return (sum(flags) / len(flags)) if flags else None


def _on_time_score(rate: float | None, on_time_target: float, weight: float) -> float:
    """Share of `weight` points earned: full marks at `on_time_target`,
    scaled linearly below it, capped at `weight` above it."""
    ratio = (rate or 0) / on_time_target if on_time_target else 0
    return round(min(ratio, 1) * weight * 100, 1)


def compute_scorecard(year: int, month: int, user=None) -> Scorecard:
    cfg = settings.KPI_SETTINGS
    start, end = month_bounds(year, month)

    requests_qs = PurchaseRequest.objects.filter(
        po_issued_at__isnull=False, po_issued_at__range=(start, end)
    )
    if user is not None:
        requests_qs = requests_qs.filter(handled_by=user)
    requests = list(requests_qs)

    po_hours = [(r.po_issued_at - r.created_at).total_seconds() / 3600 for r in requests]
    po_on_time_flags = [r.is_po_on_time for r in requests]
    avg_po_hours = sum(po_hours) / len(po_hours) if po_hours else None
    po_on_time_rate = _on_time_rate(po_on_time_flags)

    lists_qs = PickingList.objects.filter(handed_off_at__range=(start, end)).select_related("batch")
    if user is not None:
        lists_qs = lists_qs.filter(handled_by=user)
    lists = list(lists_qs)

    in_process_on_time_rate = _on_time_rate(
        [l.is_in_process_on_time for l in lists if l.is_in_process_on_time is not None]
    )

    invoiced_lists = [l for l in lists if l.hand_off_to_invoice is not None]
    invoice_on_time_rate = _on_time_rate([l.is_invoice_on_time for l in invoiced_lists])
    avg_invoice_hours = (
        sum(l.hand_off_to_invoice.total_seconds() / 3600 for l in invoiced_lists) / len(invoiced_lists)
        if invoiced_lists else None
    )

    errors_qs = BillingError.objects.filter(
        reported_at__range=(start, end), disputed=False
    ).select_related("picking_list")
    if user is not None:
        errors_qs = errors_qs.filter(picking_list__handled_by=user)
    assistant_errors = [e for e in errors_qs if e.counts_against_bonus]
    error_rate = (len(assistant_errors) / len(lists)) if lists else None

    indicators = [
        IndicatorScore(
            label=f"Orden de compra emitida dentro de {cfg['PO_TARGET_HOURS']} h desde la solicitud",
            target_label=f"≥ {cfg['PO_ON_TIME_TARGET']:.0%} a tiempo",
            actual_label=f"{po_on_time_rate:.0%}" if po_on_time_rate is not None else "—",
            weight=cfg["WEIGHT_PO_ON_TIME"] * 100,
            score=_on_time_score(po_on_time_rate, cfg["PO_ON_TIME_TARGET"], cfg["WEIGHT_PO_ON_TIME"]),
        ),
        IndicatorScore(
            label=f"Lista de picking marcada En proceso dentro de {cfg['IN_PROCESS_TARGET_HOURS']} h desde la entrega",
            target_label=f"≥ {cfg['IN_PROCESS_ON_TIME_TARGET']:.0%} a tiempo",
            actual_label=f"{in_process_on_time_rate:.0%}" if in_process_on_time_rate is not None else "—",
            weight=cfg["WEIGHT_IN_PROCESS_ON_TIME"] * 100,
            score=_on_time_score(in_process_on_time_rate, cfg["IN_PROCESS_ON_TIME_TARGET"], cfg["WEIGHT_IN_PROCESS_ON_TIME"]),
        ),
        IndicatorScore(
            label=f"Factura emitida dentro de {cfg['INVOICE_TARGET_HOURS']} h desde la entrega",
            target_label=f"≥ {cfg['INVOICE_ON_TIME_TARGET']:.0%} a tiempo",
            actual_label=f"{invoice_on_time_rate:.0%}" if invoice_on_time_rate is not None else "—",
            weight=cfg["WEIGHT_INVOICE_ON_TIME"] * 100,
            score=_on_time_score(invoice_on_time_rate, cfg["INVOICE_ON_TIME_TARGET"], cfg["WEIGHT_INVOICE_ON_TIME"]),
        ),
    ]

    if error_rate is None:
        err_score = cfg["WEIGHT_ERROR_RATE"] * 100
    else:
        overshoot = max(0.0, error_rate - cfg["ERROR_RATE_TARGET"])
        penalty_ratio = min(1.0, overshoot / cfg["ERROR_RATE_TARGET"]) if cfg["ERROR_RATE_TARGET"] else 0
        err_score = (1 - penalty_ratio) * cfg["WEIGHT_ERROR_RATE"] * 100
    indicators.append(IndicatorScore(
        label="Errores de facturación atribuibles a la asistente",
        target_label=f"≤ {cfg['ERROR_RATE_TARGET']:.0%} de las listas",
        actual_label=f"{error_rate:.1%}" if error_rate is not None else "—",
        weight=cfg["WEIGHT_ERROR_RATE"] * 100,
        score=round(err_score, 1),
    ))

    attainment = round(min(sum(i.score for i in indicators), 100.0), 1)

    if attainment < cfg["BONUS_THRESHOLD"] * 100:
        bonus_payable = 0.0
    else:
        bonus_payable = round(cfg["BASE_BONUS"] * min(attainment, 100) / 100, 2)

    return Scorecard(
        year=year,
        month=month,
        avg_po_hours=avg_po_hours,
        po_on_time_count=sum(po_on_time_flags),
        po_total_count=len(po_hours),
        avg_invoice_hours=avg_invoice_hours,
        invoice_lists_count=len(lists),
        error_rate=error_rate,
        error_count=len(assistant_errors),
        attainment=attainment,
        bonus_payable=bonus_payable,
        bonus_threshold_pct=cfg["BONUS_THRESHOLD"] * 100,
        attainment_target_pct=cfg["ATTAINMENT_TARGET"] * 100,
        indicators=indicators,
        errors=list(errors_qs),
    )
