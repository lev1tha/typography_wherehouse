import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";

import api from "../api/api.js";
import { apiError } from "../api/errors.js";
import Field from "./Field.jsx";
import { useUI } from "./UIProvider.jsx";
import { formatNumber } from "../utils/format.js";

// Виды услуг, у которых есть площадь и значит бывает коэффициент толщины.
const KINDS = ["CUTTING", "ENGRAVING", "INSTALL_INTERIOR"];

// Таблица «вид услуги × толщина → коэффициент» (CALC-02): как ВПР с приближённым
// совпадением — строка действует для толщин от «от, мм» и выше, пока её не
// перебьёт строка с большей границей. Ставка работы умножается на коэффициент;
// ставка из матрицы и вписанная вручную — нет.
export default function ThicknessCoefCard() {
  const { t } = useTranslation();
  const { toast, confirm } = useUI();
  const [rows, setRows] = useState([]);
  const [form, setForm] = useState({ kind: "CUTTING", thickness_from: "", coefficient: "" });
  const [busy, setBusy] = useState(false);

  function load() {
    api.get("/services/thickness-coefficients/", { params: { page_size: 200 } }).then((r) =>
      setRows(r.data.results || r.data || [])
    );
  }
  useEffect(load, []);

  async function add() {
    if (form.thickness_from === "" || !(Number(form.thickness_from) >= 0))
      return toast(t("pricing.matrixNeedThickness"), "error");
    if (!(Number(form.coefficient) > 0)) return toast(t("pricing.coefNeed"), "error");
    setBusy(true);
    try {
      await api.post("/services/thickness-coefficients/", form);
      setForm({ ...form, thickness_from: "", coefficient: "" });
      load();
      toast(t("common.saved"));
    } catch (e) {
      toast(apiError(e, t("common.error")), "error");
    } finally {
      setBusy(false);
    }
  }

  async function remove(row) {
    const name = `${t(`serviceKind.${row.kind}`)}, ${t("pricing.coefFrom", { n: formatNumber(row.thickness_from, { max: 2 }) })}`;
    if (!(await confirm(t("pricing.coefConfirmDelete", { name })))) return;
    setBusy(true);
    try {
      await api.delete(`/services/thickness-coefficients/${row.id}/`);
      load();
    } catch (e) {
      toast(apiError(e, t("common.error")), "error");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="card" style={{ marginBottom: 14 }}>
      <h3 style={{ marginTop: 0 }}>{t("pricing.coefTitle")}</h3>
      <p className="muted" style={{ fontSize: 12, marginTop: 0 }}>{t("pricing.coefHint")}</p>
      {rows.length === 0 && <div className="muted">{t("common.empty")}</div>}
      {rows.map((r) => (
        <div className="recipe-row" key={r.id}>
          <span>{t(`serviceKind.${r.kind}`)}, {t("pricing.coefFrom", { n: formatNumber(r.thickness_from, { max: 2 }) })}</span>
          <span className="muted recipe-qty">
            × {formatNumber(r.coefficient, { max: 3 })}
            <button
              type="button"
              className="ghost"
              style={{ marginLeft: 8, padding: "0 6px", height: "auto" }}
              aria-label={t("common.delete")}
              disabled={busy}
              onClick={() => remove(r)}
            >
              ×
            </button>
          </span>
        </div>
      ))}
      <div className="row" style={{ marginTop: 8, alignItems: "flex-end" }}>
        <Field className="grow" style={{ margin: 0 }} label={t("pricing.serviceKindLabel")}>
          <select value={form.kind} onChange={(e) => setForm({ ...form, kind: e.target.value })}>
            {KINDS.map((k) => (
              <option key={k} value={k}>{t(`serviceKind.${k}`)}</option>
            ))}
          </select>
        </Field>
        <Field className="grow" style={{ margin: 0 }} label={t("pricing.matrixThicknessFrom")}>
          <input
            type="number"
            inputMode="decimal"
            min="0"
            step="0.01"
            value={form.thickness_from}
            onChange={(e) => setForm({ ...form, thickness_from: e.target.value })}
          />
        </Field>
        <Field className="grow" style={{ margin: 0 }} label={t("pricing.coefValue")}>
          <input
            type="number"
            inputMode="decimal"
            min="0"
            step="0.001"
            value={form.coefficient}
            onChange={(e) => setForm({ ...form, coefficient: e.target.value })}
          />
        </Field>
        <button type="button" onClick={add} disabled={busy}>+ {t("common.add")}</button>
      </div>
    </div>
  );
}
