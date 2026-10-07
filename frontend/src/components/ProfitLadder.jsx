import { useTranslation } from "react-i18next";

import Hint from "./Hint.jsx";

const som = (n) => `${Math.round(Number(n) || 0).toLocaleString("ru-RU")} сом`;
const pctFmt = (v) => (v === null || v === undefined ? "—" : `${Number(v).toLocaleString("ru-RU")} %`);

// «Как сложилась прибыль» — ОПиУ выбранного периода лесенкой (2026-10-07):
// выручка → себестоимость и потери → валовая прибыль → расходы → EBITDA →
// амортизация → операционная прибыль → проценты и налог → чистая прибыль.
// Те же цифры, что вкладка «ОПиУ» за этот месяц (`report.pnl` с сервера), у
// каждого шага — подсказка простыми словами и маржа у итогов.
export default function ProfitLadder({ pnl }) {
  const { t } = useTranslation();
  if (!pnl) return null;

  // [ключ подсказки, подпись, сумма (расход — со знаком минус), итог?, маржа]
  const steps = [
    ["revenue", t("ladder.revenue"), pnl.revenue, true],
    ["cogs", t("ladder.cogs"), -(Number(pnl.cogs_material) + Number(pnl.cogs_services))],
    ["losses", t("ladder.losses"), -pnl.losses, false, null, true],
    ["gross", t("ladder.gross"), pnl.gross_profit, true, pnl.margins?.gross],
    ["opex", t("ladder.opex"), -pnl.opex?.total],
    ["opex_cash_manual", t("ladder.manual"), -pnl.opex_cash_manual, false, null, true],
    ["cash_count", t("ladder.cashCount"), pnl.cash_count, false, null, true],
    ["ebitda", t("ladder.ebitda"), pnl.ebitda, true, pnl.margins?.ebitda],
    ["depreciation", t("ladder.depreciation"), -(Number(pnl.depreciation) + Number(pnl.disposal))],
    ["operating", t("ladder.operating"), pnl.operating_profit, true, pnl.margins?.operating],
    ["interest", t("ladder.interest"), -pnl.interest, false, null, true],
    ["tax", pnl.tax_label, -pnl.tax],
    ["net", t("ladder.net"), pnl.net_profit, true, pnl.margins?.net],
  ];

  return (
    <div className="card" style={{ marginTop: 16 }}>
      <h3 style={{ marginBottom: 2 }}>{t("ladder.title")}</h3>
      <p className="muted" style={{ fontSize: 13, marginTop: 0 }}>{t("ladder.hint")}</p>
      {steps.map(([key, label, value, isTotal, margin, optional]) => {
        if (optional && Math.round(Number(value) || 0) === 0) return null;
        const v = Number(value) || 0;
        return (
          <div
            key={key}
            className={isTotal ? "crow mat-result" : "crow"}
            style={key === "net" ? { fontSize: "1.05rem" } : undefined}
          >
            <span className="k">
              {isTotal ? <strong>{label}</strong> : label}
              <Hint text={t(`terms.${key}`, { defaultValue: "" })} />
            </span>
            <span style={{ display: "flex", gap: 10, alignItems: "baseline" }}>
              {isTotal && margin !== undefined && key !== "revenue" && (
                <span className="muted" style={{ fontSize: 12 }}>{t("ladder.margin", { pct: pctFmt(margin) })}</span>
              )}
              {isTotal ? (
                <strong style={{ color: key === "revenue" ? undefined : v >= 0 ? "var(--ok)" : "var(--danger)" }}>
                  {som(v)}
                </strong>
              ) : (
                <span style={v < 0 ? { color: "var(--danger)" } : undefined}>
                  {Math.round(v) === 0 ? som(0) : v < 0 ? `− ${som(-v)}` : `+ ${som(v)}`}
                </span>
              )}
            </span>
          </div>
        );
      })}
      {Number(pnl.capex_purchases) > 0 && (
        <p className="muted" style={{ fontSize: 12, margin: "8px 0 0" }}>
          {t("ladder.capexNote", { value: som(pnl.capex_purchases) })}
        </p>
      )}
      {Number(pnl.losses_unknown || 0) > 0 && (
        <p className="muted" style={{ fontSize: 12, margin: "4px 0 0" }}>
          {t("statements.lossesUnknown", { n: pnl.losses_unknown })}
        </p>
      )}
    </div>
  );
}
