import { useEffect, useMemo, useState } from "react";
import { useTranslation } from "react-i18next";

import api from "../../api/api.js";

const COLORS = ["#e8853a", "#ffc592", "#2a9d99", "#d6b6f6", "#7a4a1e", "#1aae39"];

function Stat({ label, value, suffix, color }) {
  return (
    <div className="stat">
      <div className="label">{label}</div>
      <div className="value" style={color ? { color: `var(--${color})` } : undefined}>
        {value}
        {suffix ? <span className="muted" style={{ fontSize: "1rem" }}> {suffix}</span> : null}
      </div>
    </div>
  );
}

// Dependency-free SVG donut.
function Donut({ segments }) {
  const total = segments.reduce((s, x) => s + x.value, 0);
  const r = 52;
  const c = 2 * Math.PI * r;
  let offset = 0;
  return (
    <svg width="130" height="130" viewBox="0 0 130 130">
      <g transform="translate(65,65) rotate(-90)">
        <circle r={r} fill="none" stroke="var(--canvas)" strokeWidth="16" />
        {total > 0 &&
          segments.map((seg, i) => {
            const frac = seg.value / total;
            const dash = frac * c;
            const el = (
              <circle
                key={i}
                r={r}
                fill="none"
                stroke={seg.color}
                strokeWidth="16"
                strokeDasharray={`${dash} ${c - dash}`}
                strokeDashoffset={-offset}
              />
            );
            offset += dash;
            return el;
          })}
      </g>
    </svg>
  );
}

export default function Dashboard() {
  const { t } = useTranslation();
  const [data, setData] = useState(null);
  const [materials, setMaterials] = useState([]);
  const [clientBuys, setClientBuys] = useState([]);
  const [fin, setFin] = useState(null);
  const [error, setError] = useState("");
  const [from, setFrom] = useState("");
  const [to, setTo] = useState("");

  function loadDashboard() {
    const params = {};
    if (from) params.date_from = from;
    if (to) params.date_to = to;
    api.get("/audit/dashboard/", { params }).then((r) => setData(r.data)).catch(() => setError(t("common.error")));
    api.get("/audit/client-purchases/", { params: { ordering: "-material_spend" } })
      .then((r) => setClientBuys(r.data)).catch(() => {});
  }

  useEffect(() => {
    api.get("/warehouse/materials/", { params: { ordering: "name" } }).then((r) => setMaterials(r.data.results));
    api.get("/finance/report/").then((r) => setFin(r.data)).catch(() => {});
  }, []);

  useEffect(() => {
    loadDashboard();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [from, to]);

  const byCategory = useMemo(() => {
    const map = {};
    for (const m of materials) {
      const v = Number(m.stock_value || 0);
      map[m.category] = (map[m.category] || 0) + v;
    }
    return Object.entries(map)
      .map(([category, value]) => ({ category, value }))
      .sort((a, b) => b.value - a.value);
  }, [materials]);

  if (error) return <div className="error">{error}</div>;
  if (!data) return <p className="muted">{t("common.loading")}</p>;

  const som = (v) => `${Math.round(Number(v) || 0).toLocaleString("ru-RU")} сом`;
  const rev = data.revenue;
  const revTotal = Number(rev.total);
  const maxCat = Math.max(1, ...byCategory.map((x) => x.value));

  const methods = [
    { key: "cash", label: t("checkout.cash"), color: COLORS[0] },
    { key: "mbank", label: t("checkout.mbank"), color: COLORS[1] },
    { key: "demirbank", label: t("checkout.demirbank"), color: COLORS[2] },
    { key: "online", label: t("checkout.online"), color: COLORS[3] },
  ];

  function downloadCsv() {
    const lines = [];
    const push = (k, v) => lines.push(`${k};${v}`);
    push(t("dashboard.period"), `${from || "…"} — ${to || "…"}`);
    push("", "");
    methods.forEach((m) => push(m.label, Math.round(Number(rev[m.key]))));
    push(t("dashboard.revenueTotal"), Math.round(revTotal));
    if (data.breakdown) {
      push(t("dashboard.workRevenue"), Math.round(Number(data.breakdown.work_revenue)));
      push(t("dashboard.materialRevenue"), Math.round(Number(data.breakdown.material_revenue)));
    }
    push(t("dashboard.services"), data.services_performed);
    push(t("dashboard.refunded"), Math.round(Number(data.refunds.total_refunded)));
    if (fin) {
      push("", "");
      push(t("finance.rent"), Math.round(Number(fin.fixed.rent)));
      push(t("finance.utilities"), Math.round(Number(fin.fixed.utilities)));
      push(t("finance.internet"), Math.round(Number(fin.fixed.internet)));
      push(t("finance.salary"), Math.round(Number(fin.fixed.salary)));
      push(t("finance.fixedOther"), Math.round(Number(fin.fixed.other)));
      push(t("expenseCat.CUTTER"), Math.round(Number(fin.variable.cutter)));
      push(t("dashboard.otherVariable"), Math.round(Number(fin.variable.other)));
      push(t("finance.expenses"), Math.round(Number(fin.total_expenses)));
      push(t("finance.investmentsTotal"), Math.round(Number(fin.investments.total)));
      push(t("finance.profit"), Math.round(Number(fin.profit)));
      push(t("finance.clientDebt"), Math.round(Number(fin.client_debt)));
    }
    const blob = new Blob(["﻿" + lines.join("\n")], { type: "text/csv" });
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = "obzor.csv";
    a.click();
  }

  return (
    <>
      <h1>{t("dashboard.title")}</h1>

      {/* Фильтр периода + экспорт */}
      <div className="toolbar" style={{ alignItems: "flex-end", gap: 10, flexWrap: "wrap" }}>
        <div className="field" style={{ margin: 0 }}>
          <label>{t("dashboard.from")}</label>
          <input type="date" value={from} onChange={(e) => setFrom(e.target.value)} />
        </div>
        <div className="field" style={{ margin: 0 }}>
          <label>{t("dashboard.to")}</label>
          <input type="date" value={to} onChange={(e) => setTo(e.target.value)} />
        </div>
        {(from || to) && (
          <button className="ghost" onClick={() => { setFrom(""); setTo(""); }}>{t("common.reset")}</button>
        )}
        <div style={{ flex: 1 }} />
        <button className="secondary" onClick={downloadCsv}>{t("finance.downloadCsv")}</button>
      </div>

      <div className="stat-grid" style={{ marginTop: 12 }}>
        <Stat label={t("dashboard.asset")} value={som(data.unrealised_asset)} />
        <Stat label={t("dashboard.revenueTotal")} value={som(revTotal)} />
        <Stat label={t("dashboard.services")} value={data.services_performed} />
        <Stat label={t("dashboard.refunded")} value={som(data.refunds.total_refunded)} />
        <Stat
          label={t("dashboard.lowStock")}
          value={data.low_stock_count}
          color={data.low_stock_count > 0 ? "danger" : undefined}
        />
      </div>

      {/* Work vs material revenue (admin-only page) */}
      {data.breakdown && (
        <div className="stat-grid">
          <Stat label={t("dashboard.workRevenue")} value={som(data.breakdown.work_revenue)} />
          <Stat label={t("dashboard.materialRevenue")} value={som(data.breakdown.material_revenue)} />
        </div>
      )}

      {fin && (
        <>
          <div className="stat-grid">
            <Stat
              label={t("finance.clientDebt")}
              value={som(fin.client_debt)}
              color={Number(fin.client_debt) > 0 ? "danger" : undefined}
            />
            <Stat label={t("finance.expenses")} value={som(fin.total_expenses)} />
            <Stat
              label={t("finance.profit")}
              value={som(fin.profit)}
              color={Number(fin.profit) >= 0 ? "ok" : "danger"}
            />
          </div>

          <div className="chart-row">
            {/* Расходы детально */}
            <div className="card">
              <h3>{t("dashboard.expensesBreakdown")}</h3>
              <div className="crow"><span className="k">{t("finance.rent")}</span><span>{som(fin.fixed.rent)}</span></div>
              <div className="crow"><span className="k">{t("finance.utilities")}</span><span>{som(fin.fixed.utilities)}</span></div>
              <div className="crow"><span className="k">{t("finance.internet")}</span><span>{som(fin.fixed.internet)}</span></div>
              <div className="crow"><span className="k">{t("finance.salary")}</span><span>{som(fin.fixed.salary)}</span></div>
              <div className="crow"><span className="k">{t("finance.fixedOther")}</span><span>{som(fin.fixed.other)}</span></div>
              <div className="crow"><span className="k">{t("expenseCat.CUTTER")}</span><span>{som(fin.variable.cutter)}</span></div>
              <div className="crow"><span className="k">{t("dashboard.otherVariable")}</span><span>{som(fin.variable.other)}</span></div>
              <div className="crow" style={{ borderTop: "1px solid var(--hairline)", marginTop: 6, paddingTop: 8 }}>
                <strong style={{ color: "var(--accent-strong)" }}>{t("finance.expenses")}</strong>
                <strong style={{ color: "var(--accent-strong)" }}>{som(fin.total_expenses)}</strong>
              </div>
            </div>

            {/* Вложения (не в прибыль) */}
            <div className="card">
              <h3>{t("finance.investmentsTitle")}</h3>
              <p className="muted" style={{ fontSize: 13, marginTop: -6 }}>{t("finance.investmentsHint")}</p>
              <div className="crow"><span className="k">{t("expenseCat.EQUIPMENT")}</span><span>{som(fin.investments.equipment)}</span></div>
              <div className="crow"><span className="k">{t("expenseCat.IMPROVEMENT")}</span><span>{som(fin.investments.improvement)}</span></div>
              <div className="crow" style={{ borderTop: "1px solid var(--hairline)", marginTop: 6, paddingTop: 8 }}>
                <strong style={{ color: "var(--accent-strong)" }}>{t("finance.investmentsTotal")}</strong>
                <strong style={{ color: "var(--accent-strong)" }}>{som(fin.investments.total)}</strong>
              </div>
            </div>
          </div>
        </>
      )}

      <div className="chart-row">
        {/* Revenue split donut (by payment method) */}
        <div className="card">
          <h3>{t("dashboard.revenueSplit")}</h3>
          {revTotal > 0 ? (
            <div className="donut-wrap">
              <Donut segments={methods.map((m) => ({ value: Number(rev[m.key]), color: m.color }))} />
              <div className="legend">
                {methods.map((m) => (
                  <div className="lg" key={m.key}>
                    <span className="dot" style={{ background: m.color }} />
                    {m.label}: <strong>{som(rev[m.key])}</strong>
                    <span className="muted">({Math.round((Number(rev[m.key]) / revTotal) * 100)}%)</span>
                  </div>
                ))}
              </div>
            </div>
          ) : (
            <p className="muted">{t("common.empty")}</p>
          )}
        </div>

        {/* Stock value by category bars */}
        <div className="card">
          <h3>{t("dashboard.stockByCategory")}</h3>
          {byCategory.length ? (
            byCategory.map((row, i) => (
              <div className="bar-row" key={row.category}>
                <div className="bar-head">
                  <span>{row.category}</span>
                  <strong>{som(row.value)}</strong>
                </div>
                <div className="bar-track">
                  <div
                    className="bar-fill"
                    style={{ width: `${(row.value / maxCat) * 100}%`, background: COLORS[i % COLORS.length] }}
                  />
                </div>
              </div>
            ))
          ) : (
            <p className="muted">{t("common.empty")}</p>
          )}
        </div>
      </div>

      {/* Материалы на исходе */}
      {data.low_stock_items?.length > 0 && (
        <div className="card" style={{ marginTop: 16 }}>
          <h3>{t("dashboard.lowStockTitle")}</h3>
          <table className="table">
            <thead>
              <tr>
                <th>{t("common.name")}</th>
                <th>{t("dashboard.remaining")}</th>
                <th>{t("dashboard.critical")}</th>
              </tr>
            </thead>
            <tbody>
              {data.low_stock_items.map((m) => (
                <tr key={m.id}>
                  <td><strong>{m.name}</strong></td>
                  <td style={{ color: "var(--danger)", fontWeight: 600 }}>
                    {Number(m.quantity).toFixed(2)} {t(`unit.${m.unit}`)}
                    {m.sheets_remaining != null ? ` · ≈${Math.round(Number(m.sheets_remaining))} ${t("warehouse.sheetsShort")}` : ""}
                  </td>
                  <td className="muted">{Number(m.critical_balance).toFixed(2)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {/* Who buys how much material */}
      <div className="card" style={{ marginTop: 16 }}>
        <h3>{t("dashboard.clientMaterials")}</h3>
        {clientBuys.length ? (
          <table className="table">
            <thead>
              <tr>
                <th>{t("common.name")}</th>
                <th>{t("clients.phone")}</th>
                <th>{t("dashboard.materialSpend")}</th>
                <th>{t("dashboard.materialQty")}</th>
                <th>{t("clients.orders")}</th>
              </tr>
            </thead>
            <tbody>
              {clientBuys.map((r) => (
                <tr key={r.client_id}>
                  <td><strong>{r.client_name}</strong></td>
                  <td className="muted">{r.phone}</td>
                  <td>{som(r.material_spend)}</td>
                  <td>{Number(r.material_qty)}</td>
                  <td>{r.orders}</td>
                </tr>
              ))}
            </tbody>
          </table>
        ) : (
          <p className="muted">{t("common.empty")}</p>
        )}
      </div>
    </>
  );
}
