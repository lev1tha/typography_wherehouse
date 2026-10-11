"""Выбор ставки работы: матрица, коэффициент по толщине, прежняя цепочка.

Одна функция на кассу, предпросмотр, дозаказ и пересчёт по прайсу — чтобы
«какая ставка у резки лазером по акрилу 3 мм» не вычислялась тремя разными
способами (раньше формула жила в трёх местах: сборщик строки, проверка ввода и
фронт). Деньги — только `Decimal`.

Порядок выбора ставки (2026-10-10, CALC-02 / CALC-05):

1. матрица `RateMatrixEntry` по МАТЕРИАЛУ — ставка именно для пары
   «услуга × материал»;
2. матрица по ТОЛЩИНЕ — строка с наибольшей границей «от», не выше толщины;
3. прежняя цепочка: у резки — ставка станка (`rate_per_pm`), если она задана,
   иначе ставка материала (`cut_rate_per_pm`); у прочих площадных услуг —
   `rate_flat`;
4. коэффициент по толщине материала (`ThicknessCoefficient`) применяется
   только к ставке УСЛУГИ — станка или `rate_flat` (S3, RP-N5, D-180). Ставка,
   которая уже зависит от материала или толщины, — ставка материала (у
   форекса 8 мм она своя, 45 против 35 у 3 мм) и матрица — явная цена, и
   коэффициент на неё не накладывается: иначе толщина считалась бы дважды.

Ручная ставка кассы (админ, или складовщик там, где ей разрешено) заменяет всю
цепочку, но множитель «проходы» к ней применяется.
"""
from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal
from typing import NamedTuple

ZERO = Decimal("0")
CENT = Decimal("0.01")

SOURCE_MATRIX_MATERIAL = "matrix_material"
SOURCE_MATRIX_THICKNESS = "matrix_thickness"
SOURCE_MACHINE = "machine"
SOURCE_MATERIAL = "material"
SOURCE_SERVICE = "service"


class ResolvedRate(NamedTuple):
    rate: Decimal  # итоговая ставка (с коэффициентом)
    base: Decimal  # ставка до коэффициента
    source: str
    coefficient: Decimal | None  # применённый коэффициент; None — не применялся


def _q(value) -> Decimal:
    return Decimal(value).quantize(CENT, rounding=ROUND_HALF_UP)


def thickness_coefficient(kind, thickness) -> Decimal | None:
    """Коэффициент вида услуги для толщины, мм; None — строки нет."""
    from .models import ThicknessCoefficient

    if not thickness or Decimal(thickness) <= 0:
        return None
    row = (
        ThicknessCoefficient.objects.filter(kind=kind, thickness_from__lte=thickness)
        .order_by("-thickness_from")
        .first()
    )
    return row.coefficient if row else None


def matrix_rate(service, material):
    """Ставка из матрицы: (ставка, источник) или None."""
    # Строк в матрице единицы, поэтому два простых запроса, без prefetch.
    from .models import RateMatrixEntry

    if material is not None:
        row = RateMatrixEntry.objects.filter(service=service, material=material).first()
        if row:
            return row.rate, SOURCE_MATRIX_MATERIAL
        thickness = material.thickness_mm
        if thickness:
            row = (
                RateMatrixEntry.objects.filter(
                    service=service, thickness_from__isnull=False,
                    thickness_from__lte=thickness,
                )
                .order_by("-thickness_from")
                .first()
            )
            if row:
                return row.rate, SOURCE_MATRIX_THICKNESS
    return None


def resolve_rate(service, material=None) -> ResolvedRate:
    """Ставка работы услуги для материала — см. порядок в докстринге модуля."""
    found = matrix_rate(service, material)
    if found is not None:
        rate, source = found
        return ResolvedRate(Decimal(rate), Decimal(rate), source, None)

    if service.uses_running_meter:
        if service.rate_per_pm:
            base, source = service.rate_per_pm, SOURCE_MACHINE
        else:
            base = material.cut_rate_per_pm if material is not None else ZERO
            source = SOURCE_MATERIAL
    else:
        base, source = service.rate_flat, SOURCE_SERVICE
    base = Decimal(base or 0)

    coef = None
    # Ставка материала уже своя для каждой толщины — коэффициент только к
    # ставке станка или услуги (RP-N5).
    if base > 0 and material is not None and source != SOURCE_MATERIAL:
        coef = thickness_coefficient(service.kind, material.thickness_mm)
    rate = _q(base * coef) if coef is not None else base
    return ResolvedRate(rate, base, source, coef)


def apply_passes(rate, passes) -> Decimal:
    """Ставка с учётом проходов: три прохода — втрое, до копейки."""
    passes = int(passes or 1)
    return _q(Decimal(rate) * passes) if passes != 1 else Decimal(rate)
