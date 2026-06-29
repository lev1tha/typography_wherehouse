import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";

import api from "../../api/api.js";
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

  function downloadCsv() {
    const head = [
      t("common.name"), t("common.category"), t("finance.colOrders"),
      t("finance.colSoldArea"), t("finance.colSoldSheets"),
      t("finance.colMatSum"), t("finance.colCutSum"), t("finance.colStock"),
    ];
    const lines = [head.join(";")];
    for (const r of matReport) {
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
  const autoRow = (label, value) => (
    <div className="crow">
      <span className="k">{label}</span>
      <span>{som(value)}</span>
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

      <div className="chart-row">
        <div className="card">
          <h3>{t("finance.materials")}</h3>
          {editRow(t("finance.stockStart"), "stock_start")}
          {editRow(t("finance.purchase"), "material_purchase")}
          {autoRow(t("finance.stockEnd") + " *", report.materials.stock_end)}
          {editRow(t("finance.transport"), "transport")}
          {editRow(t("finance.materialDebt"), "material_debt")}
          {totalRow(t("finance.totalMaterials"), report.materials.total)}
        </div>
        <div className="card">
          <h3>{t("finance.fixed")}</h3>
          {editRow(t("finance.rent"), "rent")}
          {editRow(t("finance.utilities"), "utilities")}
          {editRow(t("finance.internet"), "internet")}
          {editRow(t("finance.fixedOther"), "fixed_other")}
          {totalRow(t("finance.totalFixed"), report.fixed.total)}
        </div>
        <div className="card">
          <h3>{t("finance.variable")}</h3>
          {autoRow(t("finance.cutter"), report.variable.cutter)}
          {autoRow(t("finance.equipment"), report.variable.equipment)}
          {autoRow(t("finance.improvement"), report.variable.improvement)}
          {autoRow(t("finance.varOther"), report.variable.other)}
          {totalRow(t("finance.totalVariable"), report.variable.total)}
          <p className="muted" style={{ fontSize: 12, marginTop: 10 }}>
            {t("finance.variableHint")}
          </p>
        </div>
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

      <div className="card" style={{ marginTop: 16 }}>
        <div className="row" style={{ justifyContent: "space-between", alignItems: "center" }}>
          <h3 style={{ margin: 0 }}>{t("finance.materialReportTitle")}</h3>
          <button className="secondary" onClick={downloadCsv} disabled={!matReport.length}>
            {t("finance.downloadCsv")}
          </button>
        </div>
        <div style={{ marginTop: 12 }}>
          <DataTable columns={matColumns} rows={matReport} />
        </div>
      </div>

      <p className="muted" style={{ fontSize: 12, marginTop: 10 }}>
        * {t("finance.stockEndHint")}
      </p>
    </>
  );
}
