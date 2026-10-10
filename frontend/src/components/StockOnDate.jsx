/**
 * «Склад на дату» (STK-04, волна 2): остаток каждого материала в его единицах
 * и по закупу на конец выбранного дня. Если на эту дату снят снимок (закрытие
 * месяца или команда), цифра из снимка — замороженная; иначе — расчёт по
 * журналу склада. Источник подписан, чтобы было видно, чему верить.
 */
import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";

import api from "../api/api.js";
import { apiError } from "../api/errors.js";
import { downloadFile } from "../utils/download.js";
import { formatMoney, formatNumber } from "../utils/format.js";
import Field from "./Field.jsx";

const lastMonthEnd = () => {
  const d = new Date();
  d.setDate(0);                                     // последний день прошлого месяца
  return d.toLocaleDateString("sv-SE");
};

export default function StockOnDate() {
  const { t } = useTranslation();
  const [day, setDay] = useState(lastMonthEnd());
  const [data, setData] = useState(null);
  const [error, setError] = useState("");

  useEffect(() => {
    if (!day) return;
    api
      .get("/warehouse/materials/on-date/", { params: { date: day } })
      .then((r) => { setData(r.data); setError(""); })
      .catch((e) => { setData(null); setError(apiError(e, t("common.error"))); });
  }, [day, t]);

  return (
    <div className="card" style={{ marginTop: 16, padding: 12 }}>
      <div className="row" style={{ justifyContent: "space-between", alignItems: "flex-end", gap: 10, flexWrap: "wrap", margin: 0 }}>
        <Field style={{ margin: 0 }} label={t("stock2.onDate")}>
          <input type="date" value={day} max={new Date().toLocaleDateString("sv-SE")} onChange={(e) => setDay(e.target.value)} />
        </Field>
        <button
          type="button"
          className="secondary"
          disabled={!data?.rows?.length}
          onClick={() => downloadFile("/warehouse/materials/on-date/", { date: day, export: "csv" }, `sklad-${day}.csv`)}
        >
          {t("stock2.toExcel")}
        </button>
      </div>
      {error && <p className="field-error" role="alert">{error}</p>}
      {data && (
        <>
          <p className="muted" style={{ fontSize: 13 }}>
            {data.source === "snapshot" ? t("stock2.fromSnapshot") : t("stock2.fromCalc")}
            {" · "}{t("stock2.total")}: <strong>{formatMoney(data.value)}</strong>
          </p>
          {data.rows.length === 0 ? (
            <p className="muted">{t("stock2.emptyOnDate")}</p>
          ) : (
            <table className="table">
              <thead>
                <tr>
                  <th>{t("common.name")}</th>
                  <th>{t("stock2.stockNow")}</th>
                  <th>{t("stock2.sum")}</th>
                </tr>
              </thead>
              <tbody>
                {data.rows.map((r) => (
                  <tr key={r.id}>
                    <td>{r.name}</td>
                    <td>{formatNumber(r.units, { max: 2 })} {r.unit_label}</td>
                    <td>{formatMoney(r.value)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </>
      )}
    </div>
  );
}
