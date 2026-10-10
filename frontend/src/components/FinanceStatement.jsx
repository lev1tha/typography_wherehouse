import { useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";

import api from "../api/api.js";
import { apiError } from "../api/errors.js";
import { downloadFile } from "../utils/download.js";
import StatementTable, { statementCsv } from "./StatementTable.jsx";
import { useUI } from "./UIProvider.jsx";
import WhatIfPanel from "./WhatIfPanel.jsx";

// Вкладка «ОПиУ», «ОДДС» или «Сверка» в «Финансах»: год, таблица по месяцам, CSV.
//   kind = "pnl"       — ОПиУ, /finance/pnl/
//   kind = "cash-flow" — ОДДС, /finance/cash-flow/
//   kind = "bridge"    — сверка «чистая прибыль → чистый денежный поток», /finance/bridge/
const META = {
  pnl: { title: "statements.pnlTitle", hint: "statements.pnlHint", csv: "opiu", rows: "pnl" },
  "cash-flow": { title: "statements.cashFlowTitle", hint: "statements.cashFlowHint", csv: "odds", rows: "cf" },
  bridge: { title: "statements.bridgeTitle", hint: "statements.bridgeHint", csv: "sverka", rows: "bridge" },
};
export default function FinanceStatement({ kind }) {
  const { t } = useTranslation();
  const { toast } = useUI();
  const thisYear = new Date().getFullYear();
  const [year, setYear] = useState(thisYear);
  const [data, setData] = useState(null);
  const wanted = useRef(null);
  // Колонки «Квартал» (G2-N1) и выгрузка отчёта за период одним файлом.
  const [quarters, setQuarters] = useState(() => {
    try {
      return localStorage.getItem("financeQuarters") === "1";
    } catch {
      return false;
    }
  });
  const [exportPart, setExportPart] = useState("year");
  function toggleQuarters(value) {
    setQuarters(value);
    try {
      localStorage.setItem("financeQuarters", value ? "1" : "0");
    } catch {
      /* приватный режим — просто не запоминаем */
    }
  }
  function exportPeriod() {
    const params = exportPart === "year" ? { year } : { year, quarter: exportPart.slice(1) };
    downloadFile("/finance/export/period/", params, `otchet-${year}.csv`)
      .catch((e) => toast(apiError(e, t("common.error")), "error"));
  }

  useEffect(() => {
    // Отметка запроса: быстрые переключения года не должны оставить на экране
    // ответ устаревшего запроса.
    const key = `${kind}-${year}`;
    wanted.current = key;
    setData(null);
    api
      .get(`/finance/${kind}/`, { params: { year } })
      .then((r) => {
        if (wanted.current === key) setData(r.data);
      })
      .catch(() => toast(t("common.error"), "error"));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [kind, year]);

  const meta = META[kind];
  const title = t(meta.title);
  const hint = t(meta.hint);

  return (
    <>
    <div className="card" style={{ marginTop: 4 }}>
      <div className="row" style={{ justifyContent: "space-between", alignItems: "flex-end", gap: 10, margin: 0 }}>
        <h3 style={{ margin: 0 }}>{title}</h3>
        <div className="row" style={{ gap: 8, alignItems: "center", margin: 0 }}>
          <button className="ghost" onClick={() => setYear(year - 1)} aria-label={t("statements.prevYear")}>‹</button>
          <strong style={{ minWidth: 48, textAlign: "center" }}>{year}</strong>
          <button className="ghost" onClick={() => setYear(year + 1)} disabled={year >= thisYear} aria-label={t("statements.nextYear")}>›</button>
          <button className="secondary" disabled={!data} onClick={() => statementCsv(data, meta.csv, t, meta.rows, quarters)}>
            {t("statements.csv")}
          </button>
        </div>
      </div>
      <div className="row" style={{ gap: 12, alignItems: "center", flexWrap: "wrap", margin: "10px 0 0" }}>
        <label style={{ display: "flex", alignItems: "center", gap: 8 }}>
          <input
            type="checkbox" style={{ width: 20, height: 20, minHeight: 0 }}
            checked={quarters} onChange={(e) => toggleQuarters(e.target.checked)}
          />
          {t("statements.showQuarters")}
        </label>
        <span className="row" style={{ gap: 6, alignItems: "center", margin: 0 }}>
          <select aria-label={t("statements.exportPart")} value={exportPart} onChange={(e) => setExportPart(e.target.value)}
            style={{ minWidth: 0, width: 150 }}>
            <option value="year">{t("statements.exportYear", { year })}</option>
            {[1, 2, 3, 4].map((q) => <option key={q} value={`q${q}`}>{t("statements.exportQuarter", { n: q })}</option>)}
          </select>
          <button className="secondary" onClick={exportPeriod}>{t("statements.exportPeriod")}</button>
        </span>
      </div>
      <p className="muted" style={{ fontSize: 13, margin: "8px 0 0" }}>{hint}</p>
      <p className="muted" style={{ fontSize: 12, margin: "4px 0 0" }}>{t("statements.currencyNote")}</p>
      {data ? (
        <>
          <StatementTable data={data} kind={meta.rows} showQuarters={quarters} />
          {kind === "cash-flow" && data.balanced === false && (
            <p style={{ fontSize: 13, marginTop: 8, color: "var(--danger-ink)" }}>{t("statements.notBalanced")}</p>
          )}
          {kind === "pnl" && Number(data.losses_unknown || 0) > 0 && (
            <p className="muted" style={{ fontSize: 12, marginTop: 8 }}>
              {t("statements.lossesUnknown", { n: data.losses_unknown })}
            </p>
          )}
        </>
      ) : (
        <p className="muted" style={{ marginTop: 12 }}>{t("common.loading")}</p>
      )}
      <p className="muted" style={{ fontSize: 12, margin: "10px 0 0" }}>{t("statements.exportHint")}</p>
    </div>
    {kind === "pnl" && <WhatIfPanel year={year} />}
    </>
  );
}
