import { useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";

import api from "../api/api.js";
import StatementTable, { statementCsv } from "./StatementTable.jsx";
import { useUI } from "./UIProvider.jsx";

// Вкладка «ОПиУ» или «ОДДС» в «Финансах»: год, таблица по месяцам, CSV.
//   kind = "pnl"       — ОПиУ, /finance/pnl/
//   kind = "cash-flow" — ОДДС, /finance/cash-flow/
export default function FinanceStatement({ kind }) {
  const { t } = useTranslation();
  const { toast } = useUI();
  const thisYear = new Date().getFullYear();
  const [year, setYear] = useState(thisYear);
  const [data, setData] = useState(null);
  const wanted = useRef(null);

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

  const title = kind === "pnl" ? t("statements.pnlTitle") : t("statements.cashFlowTitle");
  const hint = kind === "pnl" ? t("statements.pnlHint") : t("statements.cashFlowHint");

  return (
    <div className="card" style={{ marginTop: 4 }}>
      <div className="row" style={{ justifyContent: "space-between", alignItems: "flex-end", gap: 10, margin: 0 }}>
        <h3 style={{ margin: 0 }}>{title}</h3>
        <div className="row" style={{ gap: 8, alignItems: "center", margin: 0 }}>
          <button className="ghost" onClick={() => setYear(year - 1)} aria-label={t("statements.prevYear")}>‹</button>
          <strong style={{ minWidth: 48, textAlign: "center" }}>{year}</strong>
          <button className="ghost" onClick={() => setYear(year + 1)} disabled={year >= thisYear} aria-label={t("statements.nextYear")}>›</button>
          <button className="secondary" disabled={!data} onClick={() => statementCsv(data, kind === "pnl" ? "opiu" : "odds", t)}>
            {t("statements.csv")}
          </button>
        </div>
      </div>
      <p className="muted" style={{ fontSize: 13, margin: "8px 0 0" }}>{hint}</p>
      {data ? (
        <>
          <StatementTable data={data} />
          {kind === "pnl" && Number(data.losses_unknown || 0) > 0 && (
            <p className="muted" style={{ fontSize: 12, marginTop: 8 }}>
              {t("statements.lossesUnknown", { n: data.losses_unknown })}
            </p>
          )}
        </>
      ) : (
        <p className="muted" style={{ marginTop: 12 }}>{t("common.loading")}</p>
      )}
    </div>
  );
}
