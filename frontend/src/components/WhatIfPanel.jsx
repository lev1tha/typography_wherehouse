import { useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";

import api from "../api/api.js";
import { apiError } from "../api/errors.js";
import Field from "./Field.jsx";
import { useUI } from "./UIProvider.jsx";
import { formatMoney, formatNumber } from "../utils/format.js";

const som = (n) => formatMoney(n);

// «Что если» для ОПиУ (PNL-09, G2-N4): как изменится год, если цена услуг
// вырастет на x %, а закуп — на y %. Считается на сервере и нигде не
// записывается: поменяйте проценты — таблица пересчитается.
export default function WhatIfPanel({ year }) {
  const { t } = useTranslation();
  const { toast } = useUI();
  const [price, setPrice] = useState("10");
  const [cost, setCost] = useState("0");
  const [data, setData] = useState(null);
  const wanted = useRef("");

  useEffect(() => {
    const p = Number(price.replace(",", "."));
    const c = Number(cost.replace(",", "."));
    if (!Number.isFinite(p) || !Number.isFinite(c) || price === "" || cost === "") return undefined;
    const key = `${year}|${p}|${c}`;
    wanted.current = key;
    // Небольшая задержка: проценты набирают по цифре, на каждую буквально не считаем.
    const timer = setTimeout(() => {
      api.get("/finance/pnl/what-if/", { params: { year, price_pct: p, cost_pct: c } })
        .then((r) => { if (wanted.current === key) setData(r.data); })
        .catch((e) => toast(apiError(e, t("common.error")), "error"));
    }, 350);
    return () => clearTimeout(timer);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [year, price, cost]);

  const delta = (v) => {
    const n = Number(v) || 0;
    if (Math.round(n) === 0) return <span className="muted">—</span>;
    return <span style={{ color: n > 0 ? "var(--ok-ink)" : "var(--danger-ink)" }}>{n > 0 ? "+ " : "− "}{som(Math.abs(n))}</span>;
  };

  return (
    <details className="card" style={{ marginTop: 16 }}>
      <summary style={{ cursor: "pointer", fontWeight: 600, color: "var(--accent-ink)" }}>
        {t("whatIf.title")}
      </summary>
      <p className="muted" style={{ fontSize: 13, margin: "8px 0 10px" }}>{t("whatIf.hint")}</p>
      <div className="row" style={{ gap: 10, alignItems: "flex-end", flexWrap: "wrap", margin: 0 }}>
        <Field style={{ margin: 0, width: 190 }} label={t("whatIf.price")}>
          <input type="number" step="any" value={price} onChange={(e) => setPrice(e.target.value)} />
        </Field>
        <Field style={{ margin: 0, width: 190 }} label={t("whatIf.cost")}>
          <input type="number" step="any" value={cost} onChange={(e) => setCost(e.target.value)} />
        </Field>
        <button className="secondary" onClick={() => { setPrice("0"); setCost("0"); }}>{t("whatIf.reset")}</button>
      </div>
      {data && (
        <>
          <div className="table-wrap" style={{ marginTop: 12 }}>
            <table className="table">
              <thead>
                <tr>
                  <th scope="col">{t("statements.article")}</th>
                  <th scope="col">{t("whatIf.now")}</th>
                  <th scope="col">{t("whatIf.scenario")}</th>
                  <th scope="col">{t("whatIf.diff")}</th>
                </tr>
              </thead>
              <tbody>
                {data.rows.map((r) => (
                  <tr key={r.key}>
                    <td><strong>{t(`whatIf.row_${r.key}`)}</strong></td>
                    <td>{som(r.base_total)}</td>
                    <td>{som(r.scenario_total)}</td>
                    <td>{delta(r.delta_total)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          {data.margin && (data.margin.base != null || data.margin.scenario != null) && (
            <p className="muted" style={{ fontSize: 13, margin: "8px 0 0" }}>
              {t("whatIf.margin", {
                now: data.margin.base != null ? `${formatNumber(data.margin.base, { max: 1 })} %` : "—",
                scenario: data.margin.scenario != null ? `${formatNumber(data.margin.scenario, { max: 1 })} %` : "—",
              })}
            </p>
          )}
          <p className="muted" style={{ fontSize: 12, margin: "6px 0 0" }}>{t("whatIf.assumptions")}</p>
        </>
      )}
    </details>
  );
}
