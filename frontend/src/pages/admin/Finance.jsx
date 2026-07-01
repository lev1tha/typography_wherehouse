import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";

import api from "../../api/api.js";
import DailyProfitChart from "../../components/DailyProfitChart.jsx";
import DataTable from "../../components/DataTable.jsx";
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

  const loadReport = () => api.get("/finance/report/").then((r) => setReport(r.data));
  function load() {
    loadReport();
    api.get("/finance/settings/").then((r) => setSettings(r.data));
    api.get("/finance/material-report/").then((r) => setMatReport(r.data.rows));
  }
  // eslint-disable-next-line react-hooks/exhaustive-deps
  useEffect(() => { load(); }, []);

  function saveField(field, value) {
    api
      .patch("/finance/settings/", { [field]: value === "" ? 0 : Number(value) })
      .then(loadReport)
      .catch(() => toast(t("common.error"), "error"));
  }

  function saveText(field, value) {
    api
      .patch("/finance/settings/", { [field]: value })
      .then(loadReport)
      .catch(() => toast(t("common.error"), "error"));
  }

  const filteredMat = matFilter
    ? matReport.filter((r) => String(r.id) === matFilter)
    : matReport;

  function downloadCsv() {
    const head = [
      t("common.name"), t("common.category"), t("finance.colOrders"),
      t("finance.colSoldArea"), t("finance.colSoldSheets"),
      t("finance.colMatSum"), t("finance.colCutSum"), t("finance.colStock"),
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
        Number(r.stock || 0).toFixed(2),
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
  const noteRow = (label, field) => (
    <div className="crow" key={field}>
      <span className="k" style={{ color: "var(--ink-muted)", fontSize: 13 }}>{label}</span>
      <input
        type="text"
        value={settings[field] ?? ""}
        onChange={(e) => setSettings({ ...settings, [field]: e.target.value })}
        onBlur={(e) => saveText(field, e.target.value)}
        placeholder={t("finance.notePlaceholder")}
        style={{ width: 220, height: 34 }}
      />
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

      <div className="card" style={{ marginTop: 16 }}>
        <h3>{t("finance.fixed")}</h3>
        {editRow(t("finance.rent"), "rent")}
        {editRow(t("finance.utilities"), "utilities")}
        {noteRow(t("finance.utilitiesNote"), "utilities_note")}
        {editRow(t("finance.internet"), "internet")}
        {editRow(t("finance.salary"), "salary")}
        {editRow(t("finance.fixedOther"), "fixed_other")}
        {noteRow(t("finance.fixedOtherNote"), "fixed_other_note")}
        {totalRow(t("finance.totalFixed"), report.fixed.total)}
      </div>

      {report.investments && (
        <div className="card" style={{ marginTop: 16 }}>
          <h3>{t("finance.investmentsTitle")}</h3>
          <p className="muted" style={{ fontSize: 13, marginTop: -6 }}>{t("finance.investmentsHint")}</p>
          <div className="crow"><span className="k">{t("expenseCat.EQUIPMENT")}</span><span>{som(report.investments.equipment)}</span></div>
          <div className="crow"><span className="k">{t("expenseCat.IMPROVEMENT")}</span><span>{som(report.investments.improvement)}</span></div>
          {totalRow(t("finance.investmentsTotal"), report.investments.total)}
        </div>
      )}

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
        </div>
      </details>
    </>
  );
}
