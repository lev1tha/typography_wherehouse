/**
 * Responsive data display: a real <table> on desktop, a stack of cards under
 * 600px (CSS toggles which is visible). Avoids horizontal scroll on phones.
 *
 * columns: [{ key, label, render?(row), sortKey? }]
 *   sortKey — when set (and onSort given), the header is clickable to sort.
 * rowClass?(row) -> string for conditional highlighting (e.g. low stock)
 * sort: { key, dir }  — the currently active sort (controlled by the parent)
 * onSort?(sortKey)    — called when a sortable header is clicked
 */
import { useTranslation } from "react-i18next";

import Icon from "./Icon.jsx";

export default function DataTable({ columns, rows, rowKey = "id", rowClass, empty, sort, onSort }) {
  const { t } = useTranslation();

  if (!rows?.length) {
    return (
      <div className="empty-state">
        <Icon name="archive" size={40} className="es-icon" />
        {empty || t("common.empty")}
      </div>
    );
  }

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
      <table className="table">
        <thead>
          <tr>
            {columns.map((c) => (
              <th key={c.key} className={c.sortKey && onSort ? "sortable" : ""}>
                {header(c)}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr key={row[rowKey]} className={rowClass?.(row) || ""}>
              {columns.map((c) => (
                <td key={c.key}>{cell(c, row)}</td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>

      <div className="cards">
        {rows.map((row) => (
          <div key={row[rowKey]} className={`data-card ${rowClass?.(row) || ""}`}>
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
