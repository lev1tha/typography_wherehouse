/**
 * Responsive data display: a real <table> on desktop, a stack of cards under
 * 600px (CSS toggles which is visible). Avoids horizontal scroll on phones.
 *
 * columns: [{ key, label, render?(row) }]
 * rowClass?(row) -> string for conditional highlighting (e.g. low stock)
 */
import { useTranslation } from "react-i18next";

import Icon from "./Icon.jsx";

export default function DataTable({ columns, rows, rowKey = "id", rowClass, empty }) {
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

  return (
    <>
      <table className="table">
        <thead>
          <tr>
            {columns.map((c) => (
              <th key={c.key}>{c.label}</th>
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
