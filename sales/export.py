"""CSV «Чеки со строками» (волна 2): одна строка файла — одна позиция чека.

Формат «в Excel», как у остальных выгрузок (`finance.exports`, `clients.export`):
разделитель «;», дробная часть через запятую, BOM, даты ДД.ММ.ГГГГ, все страницы
сразу, ячейки, похожие на формулу, экранированы. Себестоимость — только тем, кто
видит закупку (админ и бухгалтер, как во всём API — D-118); складовщику колонки
нет вовсе.
"""
from __future__ import annotations

from decimal import Decimal

from django.utils import timezone

from clients.export import safe
from finance.exports import csv_response

from .models import TransactionItem


def _size(item: TransactionItem) -> str:
    if item.width is None or item.length is None:
        return ""
    n = lambda v: format(Decimal(v).normalize(), "f").replace(".", ",")  # noqa: E731
    parts = f" ×{item.parts_count}" if (item.parts_count or 1) > 1 else ""
    return f"{n(item.width)}×{n(item.length)}{parts}"


def receipts_csv(receipts, *, with_cost: bool):
    from .serializers import TransactionItemSerializer

    units = TransactionItemSerializer()
    header = [
        "Заказ №", "Дата заказа", "Клиент", "Телефон", "Наименование заказа", "Позиция",
        "Услуга", "Материал", "Размеры, м", "Количество", "Ед.", "Цена, сом", "Сумма, сом",
        "Возвращено", "Исполнитель", "Оформил", "Статус оплаты",
    ]
    if with_cost:
        header.append("Себестоимость, сом")
    rows = [header]
    for r in receipts:
        head = [
            r.order_number, timezone.localtime(r.created_at).date(),
            safe(r.client.display_name if r.client_id else r.buyer_name),
            safe(r.client.phone if r.client_id else ""), safe(r.title),
        ]
        tail_receipt = [r.get_payment_status_display()]
        items = list(r.items.all())
        if not items:
            rows.append(head + [""] * 10 + [safe(r.cashier.username if r.cashier_id else "")] + tail_receipt
                        + ([""] if with_cost else []))
            continue
        for it in items:
            row = head + [
                it.get_type_display(),
                safe(it.service.name if it.service_id else ""),
                safe(it.material.name if it.material_id else (it.work_material.name if it.work_material_id else "")),
                _size(it),
                format(it.quantity.normalize(), "f").replace(".", ","),
                units.get_unit_label(it),
                it.price_per_item,
                it.sold_total,
                it.is_returned,
                safe(it.executor.full_name if it.executor_id else ""),
                safe(r.cashier.username if r.cashier_id else ""),
            ] + tail_receipt
            if with_cost:
                row.append(it.cost_total or Decimal("0"))
            rows.append(row)
    stamp = timezone.localdate().strftime("%Y-%m-%d")
    return csv_response(rows, f"cheki-{stamp}.csv")
