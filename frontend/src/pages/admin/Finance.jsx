import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";

import api from "../../api/api.js";
import DailyProfitChart from "../../components/DailyProfitChart.jsx";
import DataTable from "../../components/DataTable.jsx";
import MonthPicker from "../../components/MonthPicker.jsx";
import { useUI } from "../../components/UIProvider.jsx";

const som = (n) => `${Math.round(Number(n) || 0).toLocaleString("ru-RU")} сом`;
const q2 = (n) => Number(n || 0).toLocaleString("ru-RU", { maximumFractionDigits: 2 });
const CAT = { forex: "Форекс", alukobond: "Алюкобонд", acryl: "Акрил", other: "Прочее" };

function Stat({ label, value, color }) {
  return (
    <div className="stat">
      <div className="label">{label}</div>
      <div className="value" style={color ? { color: `var(--${color})` } : undefined}>
        {value}
      </div>
    </div>
  );
}

export default function Finance({ embedded = false }) {
  const { t } = useTranslation();
  const { toast } = useUI();
  const [report, setReport] = useState(null);
  const [settings, setSettings] = useState(null);
  const [matReport, setMatReport] = useState([]);
  const [matFilter, setMatFilter] = useState("");
  const [matTotals, setMatTotals] = useState(null);
  // Период для отчёта по материалам: месяц или конкретный день.
  const [matPeriod, setMatPeriod] = useState({ year: new Date().getFullYear(), month: null });
  const [matDay, setMatDay] = useState("");

  const loadReport = () => api.get("/finance/report/").then((r) => setReport(r.data));
  function load() {
    loadReport();
    api.get("/finance/settings/").then((r) => setSettings(r.data));
    loadMaterialReport();
  }
  // eslint-disable-next-line react-hooks/exhaustive-deps
  useEffect(() => { load(); }, []);

  function saveField(field, value) {
    api
      .patch("/finance/settings/", { [field]: value === "" ? 0 : Number(value) })
      .then(loadReport)
      .catch(() => toast(t("common.error"), "error"));
  }

  function matPeriodParams() {
    if (matDay) return { date_from: matDay, date_to: matDay };
    if (!matPeriod.month) return {};
    return { year: matPeriod.year, month: matPeriod.month };
  }

  function loadMaterialReport() {
    api
      .get("/finance/material-report/", { params: matPeriodParams() })
      .then((r) => {
        setMatReport(r.data.rows);
        setMatTotals(r.data.totals || null);
      })
      .catch(() => toast(t("common.error"), "error"));
  }
  // eslint-disable-next-line react-hooks/exhaustive-deps
  useEffect(loadMaterialReport, [matPeriod.year, matPeriod.month, matDay]);

  const filteredMat = matFilter
    ? matReport.filter((r) => String(r.id) === matFilter)
    : matReport;

  function downloadCsv() {
    const head = [
      t("common.name"), t("common.category"), t("finance.colOrders"),
      t("finance.colSoldArea"), t("finance.colSoldSheets"),
      t("finance.colMatSum"), t("finance.colCutSum"), t("finance.colReceived"),
      t("finance.colStock"),
    ];
    const lines = [head.join(";")];
    for (const r of filteredMat) {
      lines.push([
        r.name,
        CAT[r.category] || r.category,
        r.orders,
        Number(r.sold_area || 0).toFixed(2),
        Number(r.sold_sheets || 0).toFixed(2),
        Math.round(Number(r.material_revenue || 0)),
        Math.round(Number(r.cut_revenue || 0)),
        Number(r.received || 0).toFixed(2),
        Number(r.stock || 0).toFixed(2),
      ].join(";"));
    }
    if (matTotals && !matFilter) {
      lines.push([
        t("finance.totalRow"), "", matTotals.orders,
        Number(matTotals.sold_area || 0).toFixed(2),
        Number(matTotals.sold_sheets || 0).toFixed(2),
        Math.round(Number(matTotals.material_revenue || 0)),
        Math.round(Number(matTotals.cut_revenue || 0)),
        Number(matTotals.received || 0).toFixed(2),
        "",
      ].join(";"));
    }
    const blob = new Blob(["﻿" + lines.join("\n")], { type: "text/csv" });
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = "rezka-po-materialam.csv";
    a.click();
  }

  const matColumns = [
    { key: "name", label: t("common.name"), render: (r) => <strong>{r.name}</strong> },
    { key: "category", label: t("common.category"), render: (r) => <span className="chip">{CAT[r.category] || r.category}</span> },
    { key: "orders", label: t("finance.colOrders") },
    { key: "sold_area", label: t("finance.colSoldArea"), render: (r) => q2(r.sold_area) },
    { key: "sold_sheets", label: t("finance.colSoldSheets"), render: (r) => q2(r.sold_sheets) },
    { key: "material_revenue", label: t("finance.colMatSum"), render: (r) => som(r.material_revenue) },
    { key: "cut_revenue", label: t("finance.colCutSum"), render: (r) => som(r.cut_revenue) },
    { key: "received", label: t("finance.colReceived"), render: (r) => q2(r.received) },
    { key: "stock", label: t("finance.colStock"), render: (r) => `${q2(r.stock)} ${t(`unit.${r.unit}`)}` },
  ];

  if (!report || !settings) return <p className="muted">{t("common.loading")}</p>;

  const editRow = (label, field) => (
    <div className="crow" key={field}>
      <span className="k">{label}</span>
      <input
        type="number"
        value={settings[field] ?? 0}
        onChange={(e) => setSettings({ ...settings, [field]: e.target.value })}
        onBlur={(e) => saveField(field, e.target.value)}
        placeholder="0"
        style={{ width: 150, height: 34, textAlign: "right" }}
      />
    </div>
  );
  // Заголовок блока и подытог — визуальное разделение как в Excel заказчика.
  const blockHead = (label) => (
    <div
      style={{
        background: "var(--primary-soft)",
        borderRadius: "var(--r-md)",
        padding: "8px 14px",
        margin: "0 0 6px",
        fontWeight: 700,
        color: "var(--accent-strong)",
      }}
    >
      {label}
    </div>
  );
  const totalRow = (label, value) => (
    <div
      className="crow"
      style={{
        background: "var(--primary-soft)",
        borderRadius: "var(--r-md)",
        padding: "10px 14px",
        marginTop: 8,
      }}
    >
      <strong style={{ color: "var(--accent-strong)" }}>{label}</strong>
      <strong style={{ color: "var(--accent-strong)" }}>{som(value)}</strong>
    </div>
  );

  return (
    <>
      {!embedded && <h1>{t("finance.title")}</h1>}
      <div className="stat-grid" style={{ marginBottom: 18 }}>
        <Stat label={t("finance.revenue")} value={som(report.revenue)} />
        <Stat label={t("finance.expenses")} value={som(report.total_expenses)} />
        <Stat
          label={t("finance.profit")}
          value={som(report.profit)}
          color={Number(report.profit) >= 0 ? "ok" : "danger"}
        />
        <Stat label={t("finance.clientDebt")} value={som(report.client_debt)} color="accent-strong" />
      </div>

      <DailyProfitChart />

      {/* Себестоимость проданного и маржа: сколько осталось от выручки после
          закупочной стоимости материала, ещё до аренды и прочих расходов. */}
      <div className="card" style={{ marginTop: 16 }}>
        <h3>{t("finance.cogsTitle")}</h3>
        <p className="muted" style={{ fontSize: 13, marginTop: -6 }}>{t("finance.cogsHint")}</p>
        <div className="crow"><span className="k">{t("finance.revenue")}</span><span>{som(report.revenue)}</span></div>
        <div className="crow">
          <span className="k">{t("finance.cogs")}</span>
          <span style={{ color: "var(--danger)" }}>− {som(report.cogs)}</span>
        </div>
        {totalRow(t("finance.grossMargin"), report.gross_margin)}
      </div>

      {/* Три блока с подытогами — структура как в Excel заказчика. */}
      {report.materials && (
        <div className="card" style={{ marginTop: 16 }}>
          {blockHead(t("finance.blockMaterials"))}
          {editRow(t("finance.stockStart"), "stock_start")}
          {editRow(t("finance.materialPurchase"), "material_purchase")}
          <div className="crow">
            <span className="k">{t("finance.stockEnd")}</span>
            <span>{som(report.materials.stock_end)}</span>
          </div>
          <div className="crow">
            <span className="k">{t("finance.transportRow")}</span>
            <span>{som(report.materials.transport)}</span>
          </div>
          {editRow(t("finance.materialDebt"), "material_debt")}
          <p className="muted" style={{ fontSize: 12, margin: "2px 0 0" }}>
            {t("finance.materialsHint")}
          </p>
          {report.materials.needs_setup && (
            <p style={{ fontSize: 12, margin: "6px 0 0", color: "var(--danger)" }}>
              {t("finance.materialsNeedSetup")}
            </p>
          )}
          {totalRow(t("finance.totalMaterials"), report.materials.total)}
        </div>
      )}

      <div className="card" style={{ marginTop: 16 }}>
        {blockHead(t("finance.blockFixed"))}
        <div className="crow"><span className="k">{t("fixedCat.RENT")}</span><span>{som(report.fixed.rent)}</span></div>
        <div className="crow"><span className="k">{t("fixedCat.UTILITIES")}</span><span>{som(report.fixed.utilities)}</span></div>
        <div className="crow"><span className="k">{t("fixedCat.INTERNET")}</span><span>{som(report.fixed.internet)}</span></div>
        <div className="crow"><span className="k">{t("finance.salary")}</span><span>{som(report.fixed.salary)}</span></div>
        <div className="crow"><span className="k">{t("fixedCat.OTHER")}</span><span>{som(report.fixed.other)}</span></div>
        {totalRow(t("finance.totalFixed"), report.fixed.total)}
      </div>

      <div className="card" style={{ marginTop: 16 }}>
        {blockHead(t("finance.blockVariable"))}
        <div className="crow"><span className="k">{t("expenseCat.CUTTER")}</span><span>{som(report.variable.cutter)}</span></div>
        <div className="crow"><span className="k">{t("expenseCat.OTHER")}</span><span>{som(report.variable.other)}</span></div>
        <div className="crow">
          <span className="k">
            {t("expenseCat.EQUIPMENT")} <span className="muted" style={{ fontSize: 12 }}>· {t("finance.notInProfit")}</span>
          </span>
          <span className="muted">{som(report.variable.equipment)}</span>
        </div>
        <div className="crow">
          <span className="k">
            {t("expenseCat.IMPROVEMENT")} <span className="muted" style={{ fontSize: 12 }}>· {t("finance.notInProfit")}</span>
          </span>
          <span className="muted">{som(report.variable.improvement)}</span>
        </div>
        {totalRow(t("finance.totalVariable"), report.variable.total)}
        {report.investments && Number(report.investments.total) > 0 && (
          <p className="muted" style={{ fontSize: 12, margin: "6px 0 0" }}>
            {t("finance.investmentsHint")} — {som(report.investments.total)}
          </p>
        )}
      </div>

      <div className="card" style={{ marginTop: 16 }}>
        <h3>{t("finance.referralTitle")}</h3>
        <p className="muted" style={{ fontSize: 13, marginTop: -6 }}>{t("finance.referralHint")}</p>
        {editRow(t("finance.referralBonus"), "referral_bonus")}
      </div>

      <div className="card" style={{ marginTop: 16 }}>
        <h3>{t("finance.cuttingTitle")}</h3>
        <div className="stat-grid">
          <Stat label={t("finance.cuttingTotal")} value={som(report.cutting?.total)} />
          <Stat label="Форекс" value={som(report.cutting?.forex)} />
          <Stat label="Алюкобонд" value={som(report.cutting?.alukobond)} />
          <Stat label="Акрил" value={som(report.cutting?.acryl)} />
          <Stat label="Прочее" value={som(report.cutting?.other)} />
        </div>
      </div>

      <details className="card" style={{ marginTop: 16 }}>
        <summary style={{ cursor: "pointer", fontWeight: 600, color: "var(--accent-strong)" }}>
          {t("finance.materialReportTitle")}
        </summary>
        <div className="row" style={{ justifyContent: "space-between", alignItems: "flex-end", marginTop: 12, gap: 10, flexWrap: "wrap" }}>
          <MonthPicker value={matPeriod} onChange={(v) => { setMatPeriod(v); setMatDay(""); }} />
          <div className="field" style={{ margin: 0 }}>
            <label>{t("clients.filterDay")}</label>
            <input type="date" value={matDay} onChange={(e) => setMatDay(e.target.value)} />
          </div>
          <div className="field" style={{ margin: 0, minWidth: 220 }}>
            <label>{t("finance.filterMaterial")}</label>
            <select value={matFilter} onChange={(e) => setMatFilter(e.target.value)}>
              <option value="">{t("common.all")}</option>
              {matReport.map((r) => (
                <option key={r.id} value={r.id}>{r.name}</option>
              ))}
            </select>
          </div>
          <button className="secondary" onClick={downloadCsv} disabled={!filteredMat.length}>
            {t("finance.downloadCsv")}
          </button>
        </div>
        <div style={{ marginTop: 12 }}>
          <DataTable columns={matColumns} rows={filteredMat} />
          {/* ИТОГО по всем материалам. При фильтре по одному материалу не
              показываем — там итог совпал бы с единственной строкой. */}
          {matTotals && !matFilter && (
            <div
              className="crow"
              style={{
                background: "var(--primary-soft)",
                borderRadius: "var(--r-md)",
                padding: "10px 14px",
                marginTop: 8,
                flexWrap: "wrap",
                gap: 12,
              }}
            >
              <strong style={{ color: "var(--accent-strong)" }}>{t("finance.totalRow")}</strong>
              <span>
                <span className="muted">{t("finance.colOrders")}:</span> <strong>{matTotals.orders}</strong>
                {" · "}
                <span className="muted">{t("finance.colSoldArea")}:</span> <strong>{q2(matTotals.sold_area)}</strong>
                {" · "}
                <span className="muted">{t("finance.colMatSum")}:</span> <strong>{som(matTotals.material_revenue)}</strong>
                {" · "}
                <span className="muted">{t("finance.colCutSum")}:</span>{" "}
                <strong style={{ color: "var(--accent-strong)" }}>{som(matTotals.cut_revenue)}</strong>
                {" · "}
                <span className="muted">{t("finance.colReceived")}:</span> <strong>{q2(matTotals.received)}</strong>
              </span>
            </div>
          )}
        </div>
      </details>
    </>
  );
}
