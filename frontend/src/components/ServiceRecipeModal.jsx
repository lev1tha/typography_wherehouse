import { useState } from "react";
import { useTranslation } from "react-i18next";

import api from "../api/api.js";
import { apiError } from "../api/errors.js";
import Modal from "./Modal.jsx";
import { useUI } from "./UIProvider.jsx";

// Технологическая карта услуги: что она СЪЕДАЕТ сверх основного материала —
// клей, крепёж, растворитель. Механизм работал с самого начала (расход
// списывается со склада и садится в себестоимость строки услуги), но завести
// строку можно было только через Django-админку: в «Ценах и услугах» карта
// показывалась и всё. Расходники — тот случай, когда «настрою потом» означает
// «не настрою никогда», а склад тем временем не сходится на клей и саморезы.
export default function ServiceRecipeModal({ service, materials, onClose, onSaved }) {
  const { t } = useTranslation();
  const { toast, confirm } = useUI();
  const [form, setForm] = useState({
    material: "",
    consumption_per_unit: "",
    consumption_mode: "PER_SQM",
  });
  const [busy, setBusy] = useState(false);

  const rows = service.recipes || [];
  // Материал, уже стоящий в карте, второй раз не предлагаем: сервер такую пару
  // всё равно отклонит (service+material уникальны), а объяснять это ошибкой
  // после нажатия — хуже, чем не показывать вовсе.
  const used = new Set(rows.map((r) => String(r.material)));
  const free = materials.filter((m) => !used.has(String(m.id)));

  async function add() {
    if (!form.material) return toast(t("recipes.needMaterial"), "error");
    if (!(Number(form.consumption_per_unit) > 0)) return toast(t("recipes.needRate"), "error");
    setBusy(true);
    try {
      await api.post("/services/recipes/", { ...form, service: service.id });
      setForm({ material: "", consumption_per_unit: "", consumption_mode: form.consumption_mode });
      onSaved?.();
      toast(t("recipes.added"));
    } catch (e) {
      toast(apiError(e, t("common.error")), "error");
    } finally {
      setBusy(false);
    }
  }

  async function remove(row) {
    if (!(await confirm(t("recipes.confirmDelete", { name: row.material_name })))) return;
    setBusy(true);
    try {
      await api.delete(`/services/recipes/${row.id}/`);
      onSaved?.();
      toast(t("recipes.removed"));
    } catch (e) {
      toast(apiError(e, t("common.error")), "error");
    } finally {
      setBusy(false);
    }
  }

  const unitOf = (mode) => (mode === "PER_SQM" ? t("recipes.perSqm") : t("recipes.perOrder"));

  return (
    <Modal
      title={`${t("pricing.recipes")}: ${service.name}`}
      onClose={onClose}
      footer={<button className="secondary" onClick={onClose}>{t("common.close")}</button>}
    >
      <p className="muted" style={{ fontSize: 13, marginTop: 0 }}>{t("recipes.hint")}</p>

      {rows.length ? (
        rows.map((r) => (
          <div className="crow" key={r.id}>
            <span className="k">{r.material_name}</span>
            <span style={{ display: "flex", gap: 10, alignItems: "center" }}>
              <span>
                {r.consumption_per_unit} {unitOf(r.consumption_mode)}
              </span>
              <button
                className="ghost row-btn row-danger"
                onClick={() => remove(r)}
                disabled={busy}
              >
                {t("common.delete")}
              </button>
            </span>
          </div>
        ))
      ) : (
        <p className="muted" style={{ margin: "6px 0" }}>{t("recipes.empty")}</p>
      )}

      <div className="field" style={{ marginTop: 14 }}>
        <label>{t("recipes.material")}</label>
        <select
          value={form.material}
          onChange={(e) => setForm({ ...form, material: e.target.value })}
        >
          <option value="">—</option>
          {free.map((m) => (
            <option key={m.id} value={m.id}>{m.name}</option>
          ))}
        </select>
      </div>

      <div className="row">
        <div className="field grow" style={{ margin: 0 }}>
          <label>{t("recipes.rate")}</label>
          <input
            type="number"
            step="any"
            value={form.consumption_per_unit}
            onChange={(e) => setForm({ ...form, consumption_per_unit: e.target.value })}
            placeholder="0.05"
          />
        </div>
        <div className="field grow" style={{ margin: 0 }}>
          <label>{t("recipes.mode")}</label>
          <select
            value={form.consumption_mode}
            onChange={(e) => setForm({ ...form, consumption_mode: e.target.value })}
          >
            <option value="PER_SQM">{t("recipes.modePerSqm")}</option>
            <option value="FIXED">{t("recipes.modeFixed")}</option>
          </select>
        </div>
      </div>
      {/* «На кв.м» считается от ПЛОЩАДИ куска, а не от метража реза: 0,1 клея
          на кв.м при куске 0,5 кв.м — это 0,05, сколько бы метров его ни
          резали. Формула одна и здесь, и в обзоре. */}
      <p className="muted" style={{ fontSize: 12, margin: "6px 0 0" }}>
        {form.consumption_mode === "PER_SQM" ? t("recipes.modePerSqmHint") : t("recipes.modeFixedHint")}
      </p>

      <button style={{ marginTop: 12 }} onClick={add} disabled={busy}>
        + {t("recipes.add")}
      </button>
    </Modal>
  );
}
