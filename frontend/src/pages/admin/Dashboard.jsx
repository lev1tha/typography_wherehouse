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

  useEffect(() => {
    api.get("/audit/dashboard/").then((r) => setData(r.data)).catch(() => setError(t("common.error")));
    api.get("/warehouse/materials/", { params: { ordering: "name" } }).then((r) => setMaterials(r.data.results));
    api.get("/audit/client-purchases/", { params: { ordering: "-material_spend" } })
      .then((r) => setClientBuys(r.data)).catch(() => {});
    api.get("/finance/report/").then((r) => setFin(r.data)).catch(() => {});
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

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

  const som = (v) => `${Number(v).toLocaleString("ru-RU")} сом`;
  const cash = Number(data.revenue.cash);
  const online = Number(data.revenue.online);
  const revTotal = cash + online;
  const maxCat = Math.max(1, ...byCategory.map((x) => x.value));

  return (
    <>
      <h1>{t("dashboard.title")}</h1>

      <div className="stat-grid">
        <Stat label={t("dashboard.asset")} value={som(data.unrealised_asset)} />
        <Stat label={t("dashboard.revenueTotal")} value={som(revTotal)} />
        <Stat label={t("dashboard.services")} value={data.services_performed} />
        <Stat label={t("dashboard.consumed")} value={Number(data.materials_consumed_by_services)} />
        <Stat label={t("dashboard.refunded")} value={som(data.refunds.total_refunded)} />
        <Stat label={t("dashboard.lowStock")} value={data.low_stock_count} />
      </div>

      {/* Work vs material revenue + master's wage (admin-only page) */}
      {data.breakdown && (
        <div className="stat-grid">
          <Stat label={t("dashboard.workRevenue")} value={som(data.breakdown.work_revenue)} />
          <Stat label={t("dashboard.materialRevenue")} value={som(data.breakdown.material_revenue)} />
          <Stat
            label={`${t("dashboard.masterWage")} (${data.breakdown.commission_percent}%)`}
            value={som(data.breakdown.master_wage)}
          />
        </div>
      )}

      {fin && (
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
      )}

      <div className="chart-row">
        {/* Revenue split donut */}
        <div className="card">
          <h3>{t("dashboard.revenueSplit")}</h3>
          {revTotal > 0 ? (
            <div className="donut-wrap">
              <Donut
                segments={[
                  { value: cash, color: COLORS[0] },
                  { value: online, color: COLORS[1] },
                ]}
              />
              <div className="legend">
                <div className="lg">
                  <span className="dot" style={{ background: COLORS[0] }} />
                  {t("checkout.cash")}: <strong>{som(cash)}</strong>
                  <span className="muted">({Math.round((cash / revTotal) * 100)}%)</span>
                </div>
                <div className="lg">
                  <span className="dot" style={{ background: COLORS[1] }} />
                  {t("checkout.online")}: <strong>{som(online)}</strong>
                  <span className="muted">({Math.round((online / revTotal) * 100)}%)</span>
                </div>
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
