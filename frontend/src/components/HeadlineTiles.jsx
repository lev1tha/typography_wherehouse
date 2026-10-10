import { useTranslation } from "react-i18next";
import { useNavigate } from "react-router-dom";

import Hint from "./Hint.jsx";
import { formatDate, formatMoney, formatNumber } from "../utils/format.js";

const som = (n) => formatMoney(n);
const signed = (n) => {
  const v = Math.round(Number(n) || 0);
  return `${v > 0 ? "+" : v < 0 ? "−" : ""}${formatMoney(Math.abs(v))}`;
};
const ru = (iso) => formatDate(iso);

// Подпись периода: «октябрь 2026», «с 10.10.2026 по 19.10.2026», «весь период».
function periodLabel(period, t, lang) {
  if (!period || period.all_time) return t("overview.allTime");
  const from = new Date(`${period.from}T12:00:00`);
  const to = new Date(`${period.to}T12:00:00`);
  const wholeMonth =
    from.getDate() === 1 &&
    to.getMonth() === from.getMonth() &&
    new Date(to.getFullYear(), to.getMonth() + 1, 0).getDate() === to.getDate();
  if (wholeMonth) {
    const locale = lang === "en" ? "en-US" : lang === "ky" ? "ky-KG" : "ru-RU";
    return from.toLocaleDateString(locale, { month: "long", year: "numeric" });
  }
  return t("overview.range", { from: ru(period.from), to: ru(period.to) });
}

// Изменение к прошлому периоду: «+600 сом (+100 %) к прошлому периоду».
// Для прибыли и денег рост — хорошо (зелёный), падение — красный.
function Change({ change, t }) {
  if (!change) return <div className="change muted">{t("overview.noCompare")}</div>;
  const d = Number(change.delta) || 0;
  const cls = d > 0 ? "up" : d < 0 ? "down" : "";
  const p = Number(change.delta_pct);
  const pct = change.delta_pct !== null && change.delta_pct !== undefined
    ? ` (${p > 0 ? "+" : p < 0 ? "−" : ""}${formatNumber(Math.abs(p), { max: 1 })} %)`
    : "";
  return (
    <div className={`change ${cls}`}>
      {signed(d)}{pct} {t("overview.vsPrev")}
      <span className="muted" style={{ fontSize: 12 }}> · {t("overview.wasBefore", { value: som(change.before) })}</span>
    </div>
  );
}

function Tile({ label, hint, value, color, period, change, sub, t }) {
  return (
    <div className="stat head-tile">
      <div className="label">{label}<Hint text={hint} /></div>
      <div className="value" style={color ? { color: `var(--${color}-ink)` } : undefined}>{value}</div>
      <div className="period">{period}</div>
      {sub ? <div className="muted" style={{ fontSize: 12, marginTop: 4 }}>{sub}</div> : null}
      <Change change={change} t={t} />
    </div>
  );
}

// Главное на одном экране (2026-10-07): выручка, чистая прибыль с маржой,
// чистый денежный поток, деньги на конец периода. У каждой плитки — период,
// «сом», подсказка и изменение к прошлому периоду; под ними — короткое
// «почему прибыль не равна деньгам» из сверки. Цифры — с сервера
// (`headline` в /audit/dashboard/), теми же функциями, что «Финансы».
export default function HeadlineTiles({ headline }) {
  const { t, i18n } = useTranslation();
  const navigate = useNavigate();
  if (!headline) return null;
  const h = headline;
  const period = periodLabel(h.period, t, i18n.language);
  const profit = Number(h.net_profit.value) || 0;
  const flow = Number(h.net_cash_flow.value) || 0;
  const accounts = h.cash_end.by_account || {};
  const received = h.received || {};

  function openBridge() {
    try {
      localStorage.setItem("financeTab", "bridge");
    } catch {
      /* приватный режим — откроется вкладка по умолчанию */
    }
    navigate("/admin/finance");
  }

  return (
    <>
      <div className="head-grid">
        <Tile
          t={t}
          label={t("overview.revenue")}
          hint={t("terms.revenueTile")}
          value={som(h.revenue.value)}
          period={period}
          change={h.revenue.change}
          sub={t("overview.received", {
            total: som(received.total),
            cash: som(received.by_account?.CASH),
            bank: som(received.by_account?.BANK),
          })}
        />
        <Tile
          t={t}
          label={t("overview.netProfit")}
          hint={t("terms.net")}
          value={som(profit)}
          color={profit >= 0 ? "ok" : "danger"}
          period={period}
          change={h.net_profit.change}
          sub={
            h.net_profit.margin !== null && h.net_profit.margin !== undefined
              ? t("overview.margin", { pct: formatNumber(h.net_profit.margin) })
              : t("overview.noMargin")
          }
        />
        <Tile
          t={t}
          label={t("overview.netCashFlow")}
          hint={t("terms.net_cash_flow")}
          value={signed(flow)}
          color={flow >= 0 ? "ok" : "danger"}
          period={period}
          change={h.net_cash_flow.change}
        />
        <Tile
          t={t}
          label={t("overview.cashEnd")}
          hint={t("terms.cash_closing")}
          value={som(h.cash_end.value)}
          period={t("overview.atEnd", { period })}
          change={h.cash_end.change}
          sub={t("overview.byAccount", { cash: som(accounts.CASH), bank: som(accounts.BANK) })}
        />
      </div>

      <div className="card why-card" style={{ marginBottom: 12 }}>
        <h3 style={{ marginBottom: 4 }}>
          {t("overview.whyTitle")}
          <Hint text={t("terms.bridge")} />
        </h3>
        <p style={{ margin: "0 0 8px" }}>
          {t("overview.whyLead", { profit: som(profit), flow: signed(flow) })}
        </p>
        {(h.why.reasons || []).length === 0 ? (
          <p className="muted" style={{ margin: 0 }}>{t("overview.whyNone")}</p>
        ) : (
          h.why.reasons.map((r) => (
            <div className="why-row" key={r.key}>
              <span>
                {/* Долг поставщикам здесь — ИЗМЕНЕНИЕ за период; рядом сам
                    остаток, иначе прирост читался как долг (RU-N6, D-166). */}
                {r.key === "payables"
                  ? t("overview.payablesChange")
                  : t(`stmtRows.bridge.${r.key}`, { defaultValue: r.label })}
                <Hint text={t(`terms.bridge_${r.key}`, { defaultValue: "" })} />
                {r.key === "payables" && r.balance !== undefined && (
                  <span className="muted" style={{ display: "block", fontSize: 12 }}>
                    {t("overview.payablesNow", { date: ru(r.balance_on), value: som(r.balance) })}
                  </span>
                )}
              </span>
              <strong style={{ color: Number(r.amount) >= 0 ? "var(--ok-ink)" : "var(--danger-ink)" }}>
                {signed(r.amount)}
              </strong>
            </div>
          ))
        )}
        {Math.round(Number(h.why.unexplained) || 0) !== 0 && (
          <p style={{ color: "var(--danger-ink)", fontSize: 13, margin: "8px 0 0" }}>
            {t("overview.unexplained", { value: signed(h.why.unexplained) })}
          </p>
        )}
        <button className="ghost" style={{ marginTop: 8, paddingLeft: 0 }} onClick={openBridge}>
          {t("overview.whyMore")}
        </button>
      </div>
    </>
  );
}
