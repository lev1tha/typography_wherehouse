/**
 * Responsive data display: a real <table> on desktop, a stack of cards under
 * 900px (CSS toggles which is visible). Avoids horizontal scroll on phones
 * and tablets.
 *
 * columns: [{ key, label, render?(row), sortKey? }]
 *   sortKey — when set (and onSort given), the header is clickable to sort.
 * rowClass?(row) -> string for conditional highlighting (e.g. low stock)
 * sort: { key, dir }  — the currently active sort (controlled by the parent)
 * onSort?(sortKey)    — called when a sortable header is clicked
 * filtered / onReset  — список пуст ПОСЛЕ поиска или фильтра: вместо общего
 *                       «Нет данных» — «Ничего не найдено» и кнопка «Сбросить
 *                       фильтры»; пустая база показывает обычный `empty`
 * onRowClick?(row)    — строка (и карточка) открывается щелчком; кнопки внутри
 *                       строки гасят щелчок сами. Для клавиатуры в строке
 *                       должна быть своя кнопка «открыть»
 */
import { useTranslation } from "react-i18next";

import Icon from "./Icon.jsx";

export default function DataTable({
  columns,
  rows,
  rowKey = "id",
  rowClass,
  empty,
  sort,
  onSort,
  filtered = false,
  onReset,
  onRowClick,
}) {
  const { t } = useTranslation();

  if (!rows?.length) {
    return (
      <div className="empty-state">
        <Icon name="archive" size={40} className="es-icon" />
        {filtered ? t("common.emptyFiltered") : empty || t("common.empty")}
        {filtered && onReset && (
          <div className="empty-action">
            <button type="button" className="secondary" onClick={onReset}>
              {t("common.resetFilters")}
            </button>
          </div>
        )}
      </div>
    );
  }

  // Строка открывается щелчком мыши, но только если нажали не на кнопку/поле
  // внутри неё. Клавиатурный путь — кнопка-стрелка в самой строке (отдельная
  // остановка Tab): делать ещё и саму строку фокусируемой значило бы вдвое
  // удлинить обход таблицы.
  const rowProps = (row) =>
    onRowClick
      ? {
          onClick: (e) => {
            if (e.target.closest("button, a, input, select, textarea, label, [role='button']")) return;
            onRowClick(row);
          },
        }
      : {};

  const cell = (col, row) => (col.render ? col.render(row) : row[col.key]);

  const header = (c) => {
    if (!c.sortKey || !onSort) return c.label;
    const active = sort?.key === c.sortKey;
    return (
      <button type="button" className="th-sort" onClick={() => onSort(c.sortKey)}>
        {c.label}
        <span className={`th-arrow${active ? " active" : ""}`} aria-hidden="true">
          {active ? (sort.dir === "asc" ? "▲" : "▼") : "↕"}
        </span>
      </button>
    );
  };

  return (
    <>
      {/* Таблица в собственной прокручиваемой обёртке: в кыргызской раскладке
          подписи длиннее, ряд колонок не влезал на 21px, и вбок уезжала вся
          страница вместе с шапкой. Теперь прокручивается только таблица. */}
      <div className="table-wrap">
      <table className="table">
        <thead>
          <tr>
            {columns.map((c) => (
              <th
                key={c.key}
                scope="col"
                className={c.sortKey && onSort ? "sortable" : ""}
                aria-sort={
                  c.sortKey && onSort && sort?.key === c.sortKey
                    ? sort.dir === "asc"
                      ? "ascending"
                      : "descending"
                    : undefined
                }
              >
                {header(c)}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr
              key={row[rowKey]}
              className={`${rowClass?.(row) || ""}${onRowClick ? " clickable" : ""}`.trim()}
              {...rowProps(row)}
            >
              {columns.map((c) => (
                <td key={c.key}>{cell(c, row)}</td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
      </div>

      <div className="cards">
        {rows.map((row) => (
          <div
            key={row[rowKey]}
            className={`data-card ${rowClass?.(row) || ""}${onRowClick ? " clickable" : ""}`}
            {...rowProps(row)}
          >
            {columns.map((c) => (
              <div className="crow" key={c.key}>
                <span className="k">{c.label}</span>
                <span>{cell(c, row)}</span>
              </div>
            ))}
          </div>
        ))}
      </div>
    </>
  );
}
