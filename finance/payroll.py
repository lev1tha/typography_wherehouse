"""Расчёт ведомости зарплаты (аудит STAFF-01/-02/-09/-10, cash-11, G2-N2).

Три слоя, каждый можно подменить отдельно:

1. ВЫРАБОТКА — `output(d_from, d_to)`: какие работы кто сделал и на сколько.
   Кому достаётся строка заказа, решает ОДНА функция `executor_of(line)`:
   первым читается поле «исполнитель» строки (`TransactionItem.executor`, FK
   на `accounts.Employee`, волна 2 — выбирается в кассе). Строка без него
   (старые заказы, исполнитель не выбран) — прежний признак:
     1) сотрудник, привязанный к учётной записи кассира, оформившего заказ;
     2) иначе — единственный работающий сотрудник со «станком по умолчанию» =
        станок услуги (мастера под общими логинами «Чпу»/«Лазер»);
     3) иначе — никто (строка «Без сотрудника»: деньги выработки не теряются,
        но процентов не получает никто).
2. НАЧИСЛЕНИЕ — `calculate(month)`: оклад + проценты по правилам, действовавшим
   в этом месяце, + премия, если выработка строго больше порога. Считается на
   лету; «Начислить» (`accrue`) фиксирует результат и кладёт расход в ОПиУ.
3. ВЕДОМОСТЬ — `statement(month)`: начислено, удержано, аванс, выплачено и
   «к выдаче» по каждому человеку.

Деньги — Decimal, процент от суммы работ округляется до тыйына вверх по
половине один раз на (сотрудник, вид работы) — как весь отчётный слой
(`finance.reports.money`).
"""
from __future__ import annotations

from collections import defaultdict
from datetime import date
from decimal import Decimal

from django.db import transaction
from django.utils import timezone
from rest_framework.exceptions import ValidationError

from accounts.models import Employee
from services.models import PrintingService

from . import cash
from .auditing import fmt
from .models import CashEntry, ExpenseEntry, ExpenseKind, FinanceSettings
from .payroll_models import (
    PayRate,
    PayScheme,
    PayrollAccrual,
    PayrollAdjustment,
    PayrollPayment,
)
from .periods import ensure_month_open, ensure_open, local_day, month_end, month_start
from .reports.money import ZERO, q2
from .reports.work import service_lines

Work = PayRate.Work
Kind = PrintingService.Kind

# Метки видов работ — для ведомости и CSV (клиентский текст переводится на фронте
# по ключу `work`).
WORK_LABELS = {str(v): str(l) for v, l in Work.choices}


# --- Вид работы строки ----------------------------------------------------------


def work_of(line) -> str | None:
    """Вид работы строки заказа для оплаты; None — это не работа (отходы)."""
    svc = line.service
    if svc is None:
        return None
    if svc.kind == Kind.CUTTING:
        if svc.machine == PrintingService.Machine.CNC:
            return Work.CUTTING_CNC
        if svc.machine == PrintingService.Machine.LASER:
            return Work.CUTTING_LASER
        return Work.CUTTING
    if svc.kind == Kind.ENGRAVING:
        return Work.ENGRAVING
    if svc.kind in (Kind.INSTALL_EXTERIOR, Kind.INSTALL_INTERIOR, Kind.INSTALLATION):
        return Work.INSTALL
    if svc.kind == Kind.WASTE:
        return None          # продажа обрезков и брака — не работа мастера
    return Work.OTHER


def _rate_for(rates: dict, work: str) -> Decimal:
    """Процент вида работы: свой станочный, иначе общий для резки, иначе 0."""
    if work in rates:
        return rates[work]
    if work in (Work.CUTTING_CNC, Work.CUTTING_LASER):
        return rates.get(Work.CUTTING, ZERO)
    return ZERO


# --- Кому достаётся выработка (точка переключения) -----------------------------------


class Directory:
    """Справочник сотрудников для разбора выработки: загружается один раз."""

    def __init__(self):
        employees = list(Employee.objects.all())
        self.by_user = {e.user_id: e.id for e in employees if e.user_id}
        by_machine = defaultdict(list)
        for e in employees:
            if e.is_active and e.default_machine:
                by_machine[e.default_machine].append(e.id)
        # Станок отдаёт выработку, только если за ним один человек: делить между
        # двумя — догадка, а догадка в зарплате дороже пропуска.
        self.by_machine = {m: ids[0] for m, ids in by_machine.items() if len(ids) == 1}
        self._rates = {}

    def rates(self, employee_id, month) -> dict:
        """{вид работы: процент} правил сотрудника на месяц (кэш на справочник)."""
        key = (employee_id, month_start(month))
        if key not in self._rates:
            scheme = scheme_for(employee_id, month)
            self._rates[key] = {r.work: r.percent for r in scheme.rates.all()} if scheme else None
        return self._rates[key]

    def executor_of(self, line) -> int | None:
        explicit = getattr(line, "executor_id", None)     # исполнитель в строке (волна 2)
        if explicit:
            return explicit
        cashier = getattr(line.receipt, "cashier_id", None)
        if cashier in self.by_user:
            return self.by_user[cashier]
        machine = line.service.machine if line.service_id else ""
        return self.by_machine.get(machine)


def executor_of(line, directory: Directory | None = None) -> int | None:
    """Сотрудник, которому идёт выработка строки (None — не определён)."""
    return (directory or Directory()).executor_of(line)


def line_master_share(line, directory: Directory | None = None) -> Decimal:
    """Доля мастера в стоимости строки услуги — для маржи строки (PNL-05).

    Процент берётся из правил оплаты исполнителя на месяц продажи (его свой
    процент по виду работы); исполнителя нет или правил нет — для резки общий
    «% ЗП мастера» из настроек цен. Остальные работы без правил — ноль."""
    work = work_of(line)
    if work is None:
        return ZERO
    directory = directory or Directory()
    percent = None
    executor = directory.executor_of(line)
    if executor:
        rates = directory.rates(executor, line.receipt.revenue_recognized_at or timezone.now())
        if rates is not None:
            percent = _rate_for(rates, work)
    if percent is None and work.startswith("CUTTING"):
        from services.models import PricingSettings

        percent = PricingSettings.load().master_commission_percent or ZERO
    return q2(line.sold_total * (percent or ZERO) / 100)


# --- Выработка периода --------------------------------------------------------------


def output(d_from, d_to, directory: Directory | None = None) -> dict:
    """{id сотрудника или None: {вид работы: {"amount", "meters"}}} за период.

    `amount` — стоимость работы, как стоит в чеке (вверх до сома по строке),
    `meters` — погонные метры реза (количество строк резки).

    Гарантийная переделка (`Receipt.is_warranty`) — НЕ выработка (RF-N1,
    D-161): ни суммы, ни метров. Иначе виновник брака получал проценты и
    премию за его же исправление. Загрузку станка («Резка по станкам») она
    по-прежнему показывает — станок переделку резал."""
    directory = directory or Directory()
    result = defaultdict(lambda: defaultdict(lambda: {"amount": ZERO, "meters": ZERO}))
    for sign, line, _day in service_lines(d_from, d_to):
        work = work_of(line)
        if work is None or line.receipt.is_warranty:
            continue
        slot = result[directory.executor_of(line)][work]
        slot["amount"] += sign * line.sold_total
        if work.startswith("CUTTING"):
            slot["meters"] += sign * line.quantity
    return result


# --- Правила ----------------------------------------------------------------------------


def scheme_for(employee_id, month) -> PayScheme | None:
    """Правила, действующие в месяце: последняя запись не позже его начала."""
    return (
        PayScheme.objects.filter(employee_id=employee_id, valid_from__lte=month_start(month))
        .prefetch_related("rates").order_by("-valid_from").first()
    )


def _bonus_value(scheme, work_slots) -> Decimal:
    if scheme.bonus_metric == PayScheme.BonusMetric.RUNNING_METERS:
        return sum((s["meters"] for w, s in work_slots.items() if w.startswith("CUTTING")), ZERO)
    return sum((s["amount"] for s in work_slots.values()), ZERO)


def calculate_employee(employee_id, month, work_slots) -> dict:
    """Начисление одного человека за месяц по его правилам и выработке."""
    scheme = scheme_for(employee_id, month)
    rates = {r.work: r.percent for r in scheme.rates.all()} if scheme else {}
    lines = []
    percent_total = ZERO
    for work in Work.values:
        slot = work_slots.get(work)
        if not slot or (not slot["amount"] and not slot["meters"]):
            continue
        percent = _rate_for(rates, work)
        amount = q2(slot["amount"] * percent / 100)
        lines.append({
            "work": work, "label": WORK_LABELS[work], "base": slot["amount"],
            "meters": slot["meters"], "percent": percent, "amount": amount,
        })
        percent_total += amount
    salary = scheme.salary if scheme else ZERO
    value = _bonus_value(scheme, work_slots) if scheme else ZERO
    earned = bool(
        scheme and scheme.bonus_threshold is not None and scheme.bonus_amount
        and value > scheme.bonus_threshold
    )
    bonus = {
        "metric": scheme.bonus_metric if scheme else None,
        "threshold": scheme.bonus_threshold if scheme else None,
        "value": value,
        "earned": earned,
        "amount": scheme.bonus_amount if earned else ZERO,
    }
    gross = salary + percent_total + bonus["amount"]
    return {
        "scheme": scheme.id if scheme else None,
        "scheme_from": scheme.valid_from if scheme else None,
        "salary": salary,
        "lines": lines,
        "percent_total": percent_total,
        "bonus": bonus,
        "gross": gross,
    }


# --- Ведомость -----------------------------------------------------------------------------


def _sum(values) -> Decimal:
    return sum((Decimal(v or 0) for v in values), ZERO)


def statement(month) -> dict:
    """Ведомость за месяц: по каждому человеку и итого."""
    month = month_start(month)
    d_from, d_to = month, month_end(month)
    directory = Directory()
    out = output(d_from, d_to, directory)

    employees = {e.id: e for e in Employee.objects.select_related("user")}
    accruals = {a.employee_id: a for a in PayrollAccrual.objects.filter(month=month)}
    adjustments = defaultdict(list)
    for adj in PayrollAdjustment.objects.filter(month=month).select_related("inventory_log"):
        adjustments[adj.employee_id].append(adj)
    payments = defaultdict(list)
    for pay in PayrollPayment.objects.filter(period=month):
        payments[pay.employee_id].append(pay)

    # Кто в ведомости: все работающие, плюс любой, у кого за месяц есть деньги,
    # выработка или правила.
    ids = {e.id for e in employees.values() if e.is_active}
    ids |= set(accruals) | set(adjustments) | set(payments)
    ids |= {i for i in out if i}
    ids |= {
        pid for pid in PayScheme.objects.filter(valid_from__lte=month)
        .filter(employee__is_active=True).values_list("employee_id", flat=True)
    }

    rows = []
    for emp_id in sorted(ids, key=lambda i: (not employees[i].is_active, employees[i].full_name.lower(), i)):
        emp = employees[emp_id]
        calc = calculate_employee(emp_id, month, out.get(emp_id, {}))
        adjs = adjustments.get(emp_id, [])
        pays = payments.get(emp_id, [])
        accrual = accruals.get(emp_id)
        deductions = _sum(a.amount for a in adjs)
        advances = _sum(p.amount for p in pays if p.kind == PayrollPayment.Kind.ADVANCE)
        payouts = _sum(p.amount for p in pays if p.kind == PayrollPayment.Kind.PAYOUT)
        live_amount = max(calc["gross"] - deductions, ZERO)
        # «Начислено» — то, что проведено в ОПиУ. Не проведено — расчёт на лету
        # (помечен `posted: False`), чтобы ведомость читалась до проведения.
        accrued = accrual.amount if accrual else live_amount
        stale = bool(accrual and (accrual.gross != calc["gross"] or accrual.deductions != deductions))
        rows.append({
            "employee": {
                "id": emp.id, "name": emp.full_name, "position": emp.position,
                "is_active": emp.is_active, "machine": emp.default_machine,
            },
            **calc,
            "deductions": deductions,
            "adjustments": [
                {
                    "id": a.id, "reason": a.reason, "reason_display": a.get_reason_display(),
                    "amount": a.amount, "note": a.note, "inventory_log": a.inventory_log_id,
                }
                for a in adjs
            ],
            "posted": bool(accrual),
            "stale": stale,
            "posted_at": accrual.posted_at if accrual else None,
            "accrued": accrued,
            "advances": advances,
            "payouts": payouts,
            "paid": advances + payouts,
            "to_pay": accrued - advances - payouts,
            "payments": [
                {
                    "id": p.id, "kind": p.kind, "kind_display": p.get_kind_display(),
                    "amount": p.amount, "paid_on": p.paid_on, "account": p.account, "note": p.note,
                }
                for p in pays
            ],
        })

    unassigned = out.get(None, {})
    unassigned_rows = [
        {"work": w, "label": WORK_LABELS[w], "base": s["amount"], "meters": s["meters"]}
        for w, s in unassigned.items() if s["amount"] or s["meters"]
    ]
    totals = {
        key: _sum(r[key] for r in rows)
        for key in ("salary", "percent_total", "gross", "deductions", "accrued", "advances", "payouts", "paid", "to_pay")
    }
    totals["bonus"] = _sum(r["bonus"]["amount"] for r in rows)
    return {
        "month": month,
        "rows": rows,
        "totals": totals,
        "unassigned": unassigned_rows,
        "posted_all": all(r["posted"] for r in rows if r["gross"] or r["deductions"]) if rows else False,
        "default_period": {
            "advance": month_start(timezone.localdate()),
            "payout": default_period(PayrollPayment.Kind.PAYOUT, timezone.localdate()),
        },
    }


# --- Проведение начислений -----------------------------------------------------------------


def manual_salary_entries(month) -> list:
    """Траты вида «Зарплаты», внесённые за месяц руками или расписанием, — не
    начисления ведомости (RF-N2, D-162). Месяц — «за какой месяц», у старых
    трат без него — месяц оплаты (как их видит ОПиУ)."""
    month = month_start(month)
    qs = ExpenseEntry.objects.filter(
        kind__code=ExpenseKind.SALARY, payroll_accrual__isnull=True,
    )
    return list(
        qs.filter(period=month)
        | qs.filter(period__isnull=True, spent_at__gte=month, spent_at__lte=month_end(month))
    )


def month_has_accrual(month) -> bool:
    """Месяц ведётся ведомостью: по нему проведено начисление."""
    return PayrollAccrual.objects.filter(month=month_start(month)).exists()


@transaction.atomic
def accrue(month, user=None) -> dict:
    """Провести начисления за месяц: расход в ОПиУ без движения денег.

    Повторный вызов пересчитывает (пока месяц не закрыт замком периода): в
    ОПиУ всегда лежит последний проведённый расчёт."""
    month = month_start(month)
    ensure_month_open(month, "Начислить зарплату за этот месяц")
    kind = ExpenseKind.objects.get(code=ExpenseKind.SALARY)
    manual = manual_salary_entries(month)
    if manual:
        total = sum((e.amount for e in manual), ZERO)
        raise ValidationError({"detail": (
            f"За {month:%m.%Y} зарплата уже внесена вручную тратой «{kind.name}»: "
            f"{len(manual)} шт. на {fmt(total)} сом. Ведомость и ручная трата за один месяц "
            "задвоят расход в ОПиУ. Удалите ручные траты этого месяца в «Финансах» "
            "(выданные деньги проведите в ведомости авансом или выплатой) и начислите снова."
        )})
    stmt = statement(month)
    posted = []
    for row in stmt["rows"]:
        gross, deductions = row["gross"], row["deductions"]
        emp_id = row["employee"]["id"]
        amount = max(gross - deductions, ZERO)
        existing = PayrollAccrual.objects.filter(employee_id=emp_id, month=month).select_related("expense").first()
        if not gross and not deductions and not existing:
            continue
        accrual = existing or PayrollAccrual(employee_id=emp_id, month=month)
        accrual.gross, accrual.deductions, accrual.amount = gross, deductions, amount
        accrual.breakdown = _snapshot(row)
        accrual.posted_by = user
        entry = accrual.expense
        if amount > 0:
            if entry is None:
                entry = ExpenseEntry(kind=kind, created_by=user, is_cashless=True)
            entry.amount = amount
            entry.name = row["employee"]["name"]
            entry.note = "Начислено по ведомости"
            entry.is_cashless = True
            entry.period = month
            entry.spent_at = min(month_end(month), timezone.localdate())
            entry.save()
            accrual.expense = entry
        elif entry is not None:
            accrual.expense = None
            accrual.save()
            entry.delete()
            entry = None
        accrual.save()
        posted.append(accrual)
    return {"count": len(posted), "month": month}


def _snapshot(row: dict) -> dict:
    """Снимок расчёта в JSON: Decimal и даты — строками."""
    def fix(v):
        if isinstance(v, Decimal):
            return str(v)
        if isinstance(v, date):
            return v.isoformat()
        if isinstance(v, dict):
            return {k: fix(x) for k, x in v.items()}
        if isinstance(v, (list, tuple)):
            return [fix(x) for x in v]
        return v

    return fix({
        "salary": row["salary"], "lines": row["lines"], "bonus": row["bonus"],
        "scheme_from": row["scheme_from"], "percent_total": row["percent_total"],
    })


@transaction.atomic
def unpost(month) -> int:
    """Снять проведение месяца: убрать расход начислений из ОПиУ."""
    month = month_start(month)
    ensure_month_open(month, "Снять начисление зарплаты за этот месяц")
    n = 0
    for accrual in PayrollAccrual.objects.filter(month=month).select_related("expense"):
        entry = accrual.expense
        accrual.delete()
        if entry is not None:
            entry.delete()
        n += 1
    return n


# --- Выплаты ----------------------------------------------------------------------------------


def default_period(kind, paid_on) -> date:
    """За какой месяц по умолчанию платят: аванс — за текущий, расчёт — за прошлый,
    если выплата не позже `payroll_prev_month_until_day`-го числа."""
    paid_on = local_day(paid_on)
    current = month_start(paid_on)
    if kind == PayrollPayment.Kind.ADVANCE:
        return current
    until = FinanceSettings.load().payroll_prev_month_until_day
    if until and paid_on.day <= until:
        from .periods import add_months

        return add_months(current, -1)
    return current


@transaction.atomic
def pay(employee, *, kind, amount, paid_on=None, period=None, account="CASH", note="", user=None):
    """Выплатить аванс или расчёт: запись выплаты + расход в кассовой книге."""
    paid_on = paid_on or timezone.localdate()
    ensure_open(paid_on, "Записать выплату зарплаты этой датой")
    period = month_start(period) if period else default_period(kind, paid_on)
    payment = PayrollPayment.objects.create(
        employee=employee, period=period, kind=kind, amount=amount, paid_on=paid_on,
        account=account, note=note, created_by=user,
    )
    label = "Аванс" if kind == PayrollPayment.Kind.ADVANCE else "Зарплата"
    entry = cash.money_out(
        amount, CashEntry.Article.PAYROLL, account=account, happened_on=paid_on,
        note=f"{label}: {employee.full_name} за {period:%m.%Y}", user=user,
    )
    payment.cash_entry = entry
    payment.save(update_fields=["cash_entry"])
    return payment


@transaction.atomic
def remove_payment(payment, user=None):
    ensure_open(payment.paid_on, "Удалить выплату закрытого периода")
    entry = payment.cash_entry
    payment.delete()
    if entry is not None:
        entry.delete()


# --- CSV ---------------------------------------------------------------------------------------


def statement_csv_rows(stmt: dict) -> list[list]:
    head = ["Сотрудник", "Должность", "Оклад", "Проценты", "Премия", "Начислено (расчёт)",
            "Удержано", "Начислено в ОПиУ", "Аванс", "Выплачено", "К выдаче", "Проведено"]
    rows = [head]
    for r in stmt["rows"]:
        rows.append([
            r["employee"]["name"], r["employee"]["position"], r["salary"], r["percent_total"],
            r["bonus"]["amount"], r["gross"], r["deductions"], r["accrued"], r["advances"],
            r["payouts"], r["to_pay"], "да" if r["posted"] else "нет",
        ])
    t = stmt["totals"]
    rows.append(["ИТОГО", "", t["salary"], t["percent_total"], t["bonus"], t["gross"], t["deductions"],
                 t["accrued"], t["advances"], t["payouts"], t["to_pay"], ""])
    return rows
