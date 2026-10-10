import { useState } from "react";
import { useTranslation } from "react-i18next";

import api from "../api/api.js";
import { apiError } from "../api/errors.js";
import Field from "./Field.jsx";
import { useUI } from "./UIProvider.jsx";
import { formatNumber } from "../utils/format.js";

// Матрица ставок услуги (CALC-05): «этот материал» или «толщина от N мм» → своя
// ставка. Приоритет в кассе: строка по материалу → строка по толщине → ставка
// станка → ставка материала. Станок — это сама услуга, отдельной оси нет.
export default function RateMatrixEditor({ service, materials, onChanged }) {
  const { t } = useTranslation();
  const { toast, confirm } = useUI();
  const rows = service.rate_matrix || [];
  const [form, setForm] = useState({ by: "material", material: "", thickness_from: "", rate: "" });
  const [busy, setBusy] = useState(false);

  // Материал, уже стоящий в матрице, второй раз не предлагаем (сервер не пустит).
  const used = new Set(rows.filter((r) => r.material).map((r) => String(r.material)));
  const free = materials.filter((m) => !used.has(String(m.id)));
  const unit = service.uses_running_meter ? t("pricing.perPmShort") : t("pricing.perSqmShort");

  async function add() {
    const byMaterial = form.by === "material";
    if (byMaterial && !form.material) return toast(t("pricing.matrixNeedMaterial"), "error");
    if (!byMaterial && !(Number(form.thickness_from) >= 0 && form.thickness_from !== ""))
      return toast(t("pricing.matrixNeedThickness"), "error");
    if (form.rate === "" || !(Number(form.rate) >= 0)) return toast(t("pricing.matrixNeedRate"), "error");
    setBusy(true);
    try {
      await api.post("/services/rate-matrix/", {
        service: service.id,
        rate: form.rate,
        ...(byMaterial ? { material: form.material } : { thickness_from: form.thickness_from }),
      });
      setForm({ ...form, material: "", thickness_from: "", rate: "" });
      onChanged?.();
      toast(t("common.saved"));
    } catch (e) {
      toast(apiError(e, t("common.error")), "error");
    } finally {
      setBusy(false);
    }
  }

  async function remove(row) {
    const key = row.material_name || t("pricing.matrixThickness", { n: formatNumber(row.thickness_from, { max: 2 }) });
    if (!(await confirm(t("pricing.matrixConfirmDelete", { name: key })))) return;
    setBusy(true);
    try {
      await api.delete(`/services/rate-matrix/${row.id}/`);
      onChanged?.();
    } catch (e) {
      toast(apiError(e, t("common.error")), "error");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="field rate-matrix">
      <label>{t("pricing.matrixTitle")}</label>
      <p className="muted" style={{ fontSize: 12, margin: "0 0 6px" }}>{t("pricing.matrixHint")}</p>
      {rows.length === 0 && <div className="muted">{t("common.empty")}</div>}
      {rows.map((r) => (
        <div className="recipe-row" key={r.id}>
          <span>{r.material_name || t("pricing.matrixThickness", { n: formatNumber(r.thickness_from, { max: 2 }) })}</span>
          <span className="muted recipe-qty">
            {formatNumber(r.rate, { max: 2 })} {unit}
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
        <Field className="grow" style={{ margin: 0 }} label={t("pricing.matrixKey")}>
          <select value={form.by} onChange={(e) => setForm({ ...form, by: e.target.value })}>
            <option value="material">{t("pricing.matrixByMaterial")}</option>
            <option value="thickness">{t("pricing.matrixByThickness")}</option>
          </select>
        </Field>
        {form.by === "material" ? (
          <Field className="grow" style={{ margin: 0 }} label={t("pricing.matrixMaterial")}>
            <select value={form.material} onChange={(e) => setForm({ ...form, material: e.target.value })}>
              <option value="">—</option>
              {free.map((m) => (
                <option key={m.id} value={m.id}>{m.name}</option>
              ))}
            </select>
          </Field>
        ) : (
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
        )}
        <Field className="grow" style={{ margin: 0 }} label={`${t("pricing.matrixRate")}, ${unit}`}>
          <input
            type="number"
            inputMode="decimal"
            min="0"
            value={form.rate}
            onChange={(e) => setForm({ ...form, rate: e.target.value })}
          />
        </Field>
        <button type="button" onClick={add} disabled={busy}>+ {t("common.add")}</button>
      </div>
    </div>
  );
}
