/**
 * «К заказу» (STK-06, волна 2): материалы, упавшие до минимума, — сколько
 * докупить до двух минимумов, в листах/метрах/штуках, у кого брали в прошлый
 * раз и на какую сумму по последнему закупу. Раньше был только бейдж «на
 * исходе», а «сколько и что заказать» владелец считал в Excel.
 */
import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";

import api from "../api/api.js";
import { downloadFile } from "../utils/download.js";
import { formatMoney, formatNumber } from "../utils/format.js";

export default function ReorderPanel({ reloadKey }) {
  const { t } = useTranslation();
  const [rows, setRows] = useState([]);
  const [open, setOpen] = useState(false);

  useEffect(() => {
    api
      .get("/warehouse/materials/reorder/")
      .then((r) => setRows(r.data.results || []))
      .catch(() => setRows([]));
  }, [reloadKey]);

  if (!rows.length) return null;
  const total = rows.reduce((s, r) => s + (Number(r.sum) || 0), 0);
  const q = (n) => formatNumber(n, { max: 2 });

  return (
    <div className="card" style={{ marginBottom: 12, padding: 12 }}>
      <div className="row" style={{ justifyContent: "space-between", alignItems: "center", margin: 0 }}>
        <button
          type="button"
          className="ghost"
          aria-expanded={open}
          onClick={() => setOpen(!open)}
          style={{ padding: 0, height: "auto", fontWeight: 600 }}
        >
          {open ? "▾" : "▸"} {t("stock2.reorderTitle", { count: rows.length })}
          {total > 0 && <span className="muted" style={{ fontWeight: 400 }}> · {formatMoney(total)}</span>}
        </button>
        <button
          type="button"
          className="secondary row-btn"
          onClick={() => downloadFile("/warehouse/materials/reorder/", { export: "csv" }, "k-zakazu.csv")}
        >
          {t("stock2.toExcel")}
        </button>
      </div>
      {/* Своя прокручиваемая таблица, а не `.table`: на телефоне (до 900px)
          `.table` прячется ради карточек DataTable, а карточек у этой панели
          нет — открытый список был пустым (RU-N4, перепроверка 10.10). */}
      {open && (
        <div className="table-scroll" style={{ marginTop: 8 }}>
        <table className="table plain-table">
          <thead>
            <tr>
              <th>{t("common.name")}</th>
              <th>{t("stock2.stockNow")}</th>
              <th>{t("stock2.minimum")}</th>
              <th>{t("stock2.toOrder")}</th>
              <th>{t("stock2.supplier")}</th>
              <th>{t("stock2.sum")}</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((r) => (
              <tr key={r.id}>
                <td>{r.name}</td>
                <td>{q(r.stock)} {r.unit_label}</td>
                <td>{q(r.min)} {r.unit_label}</td>
                <td><strong>{q(r.to_order)} {r.unit_label}</strong></td>
                <td>{r.supplier || "—"}</td>
                <td>{r.sum != null ? formatMoney(r.sum) : "—"}</td>
              </tr>
            ))}
          </tbody>
        </table>
        </div>
      )}
    </div>
  );
}
