import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";

import api from "../../api/api.js";
import MonthPicker from "../../components/MonthPicker.jsx";
import { useUI } from "../../components/UIProvider.jsx";

const q2 = (n) => Number(n || 0).toLocaleString("ru-RU", { maximumFractionDigits: 2 });
// Деньги — без копеек: заказчик ведёт лист в целых сомах.
const som = (n) => Math.round(Number(n) || 0).toLocaleString("ru-RU");
const dayLabel = (iso) => `${iso.slice(8, 10)}.${iso.slice(5, 7)}`;

// Складской лист по материалам: поступление · проданные · производство ·
// деньги, плюс колонки приходов по датам.
//
// Остатков на начало и на конец месяца в листе больше нет — заказчик попросил
// убрать обе колонки (2026-08-14). Вместе с ними ушёл и ручной ввод остатка на
// начало: вписывать его было некуда и незачем, финотчёт им тоже не пользуется.
export default function MaterialStock({ embedded = false }) {
  const { t } = useTranslation();
  const { toast } = useUI();
  const now = new Date();
  const [period, setPeriod] = useState({ year: now.getFullYear(), month: now.getMonth() + 1 });
  const [rows, setRows] = useState([]);
  const [totals, setTotals] = useState(null);
  const [loading, setLoading] = useState(true);

  function load() {
    setLoading(true);
    const params = period.month ? { year: period.year, month: period.month } : {};
    api
      .get("/finance/material-report/", { params })
      .then((r) => {
        setRows(r.data.rows || []);
        setTotals(r.data.totals || null);
      })
      .catch(() => toast(t("common.error"), "error"))
      .finally(() => setLoading(false));
  }
  // eslint-disable-next-line react-hooks/exhaustive-deps
  useEffect(load, [period.year, period.month]);

  // Даты приходов по всем материалам — колонки «поступление товар» из Excel.
  const receiptDays = [
    ...new Set(rows.flatMap((r) => (r.receipts || []).map((x) => x.date))),
  ].sort();

  // Выручка с материала = продажи самого материала + резка по нему.
  const revTotal = (r) => Number(r?.material_revenue || 0) + Number(r?.cut_revenue || 0);

  function downloadCsv() {
    const num = (v) => Number(v || 0).toFixed(2);
    const money = (v) => Math.round(Number(v) || 0);
    const head = [
      t("stockSheet.colName"), t("stockSheet.colReceived"),
      t("stockSheet.colSold"), t("stockSheet.colProduction"),
      t("stockSheet.colMatRevenue"), t("stockSheet.colCutRevenue"), t("stockSheet.colRevenue"),
      ...receiptDays.map(dayLabel),
    ];
    const lines = [head.join(";")];
    for (const r of rows) {
      lines.push([
        r.name, num(r.received_qty),
        num(r.sold_qty), r.production || "",
        money(r.material_revenue), money(r.cut_revenue), money(revTotal(r)),
        ...receiptDays.map((d) => {
          const hit = (r.receipts || []).find((x) => x.date === d);
          return hit ? num(hit.qty) : "";
        }),
      ].join(";"));
    }
    if (totals) {
      lines.push([
        t("finance.totalRow"), num(totals.received_qty),
        num(totals.sold_qty), "",
        money(totals.material_revenue), money(totals.cut_revenue), money(revTotal(totals)),
      ].join(";"));
    }
    const blob = new Blob(["﻿" + lines.join("\n")], { type: "text/csv" });
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = "sklad-po-materialam.csv";
    a.click();
  }

  const num = (v) => <span className="sheet-num">{q2(v)}</span>;

  return (
    <>
      {!embedded && <h1>{t("stockSheet.title")}</h1>}
      <div className="row" style={{ justifyContent: "space-between", alignItems: "flex-end", gap: 10, flexWrap: "wrap" }}>
        <MonthPicker value={period} onChange={setPeriod} />
        <button className="secondary" onClick={downloadCsv} disabled={!rows.length}>
          {t("finance.downloadCsv")}
        </button>
      </div>
      <p className="muted" style={{ fontSize: 13 }}>{t("stockSheet.hint")}</p>

      {loading ? (
        <p className="muted">{t("common.loading")}</p>
      ) : (
        <div className="sheet-wrap">
          <table className="table sheet-table">
            <thead>
              {/* Две шапки, как в Excel: «на складе» и «поступление товар». */}
              <tr>
                <th rowSpan={2}>{t("stockSheet.colName")}</th>
                <th colSpan={3} className="sheet-group">{t("stockSheet.groupStock")}</th>
                {/* Сколько материал принёс денег — рядом с тем, сколько его
                    ушло: в листе заказчика количества и суммы живут вместе. */}
                <th colSpan={3} className="sheet-group sheet-group-money">
                  {t("stockSheet.groupMoney")}
                </th>
                {receiptDays.length > 0 && (
                  <th colSpan={receiptDays.length} className="sheet-group sheet-group-alt">
                    {t("stockSheet.groupIncoming")}
                  </th>
                )}
              </tr>
              <tr>
                {/* Остатки на начало и на конец месяца из листа убраны
                    (просьба заказчика): остаётся движение за месяц —
                    сколько пришло, сколько ушло и на сколько денег. */}
                <th>{t("stockSheet.colReceived")}</th>
                <th>{t("stockSheet.colSold")}</th>
                <th>{t("stockSheet.colProduction")}</th>
                <th>{t("stockSheet.colMatRevenue")}</th>
                <th>{t("stockSheet.colCutRevenue")}</th>
                <th>{t("stockSheet.colRevenue")}</th>
                {receiptDays.map((d) => (
                  <th key={d}>{dayLabel(d)}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {rows.map((r) => (
                <tr key={r.id}>
                  <td><strong>{r.name}</strong></td>
                  <td>{num(r.received_qty)}</td>
                  <td>{num(r.sold_qty)}</td>
                  <td>{r.production || <span className="muted">—</span>}</td>
                  <td><span className="sheet-num">{som(r.material_revenue)}</span></td>
                  <td><span className="sheet-num">{som(r.cut_revenue)}</span></td>
                  <td className="sheet-money"><span className="sheet-num">{som(revTotal(r))}</span></td>
                  {receiptDays.map((d) => {
                    const hit = (r.receipts || []).find((x) => x.date === d);
                    return <td key={d}>{hit ? num(hit.qty) : <span className="muted">—</span>}</td>;
                  })}
                </tr>
              ))}
            </tbody>
            {totals && (
              <tfoot>
                <tr className="sheet-total">
                  <td><strong>{t("finance.totalRow")}</strong></td>
                  <td>{num(totals.received_qty)}</td>
                  <td>{num(totals.sold_qty)}</td>
                  <td />
                  <td><span className="sheet-num">{som(totals.material_revenue)}</span></td>
                  <td><span className="sheet-num">{som(totals.cut_revenue)}</span></td>
                  <td><span className="sheet-num">{som(revTotal(totals))}</span></td>
                  {receiptDays.length > 0 && <td colSpan={receiptDays.length} />}
                </tr>
              </tfoot>
            )}
          </table>
        </div>
      )}
    </>
  );
}
