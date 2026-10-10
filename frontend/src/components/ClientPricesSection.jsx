import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";

import api from "../api/api.js";
import { apiError } from "../api/errors.js";
import { useUI } from "./UIProvider.jsx";
import { formatMoney } from "../utils/format.js";

// Договорные цены клиента (CLI-02, волна 2): цена на материал (за кв.м, лист
// или пог.м) или на работу (+ необязательно материал работы) — вместо каталога.
// Скидка клиента к таким строкам не применяется; вписанная в кассе цена —
// сильнее договорной. Правит админ, остальные видят.
const EMPTY = { what: "material", service: "", material: "", sale_mode: "PIECE", price: "", note: "" };

export default function ClientPricesSection({ clientId, canEdit }) {
  const { t } = useTranslation();
  const { toast, confirm } = useUI();
  const [rows, setRows] = useState([]);
  const [services, setServices] = useState([]);
  const [materials, setMaterials] = useState([]);
  const [form, setForm] = useState(null);
  const [busy, setBusy] = useState(false);
  // Правка цены строки — прямо в строке: {id, price}.
  const [editing, setEditing] = useState(null);

  function load() {
    api
      .get("/clients/client-prices/", { params: { client: clientId } })
      .then((r) => setRows(r.data || []))
      .catch(() => setRows([]));
  }
  useEffect(load, [clientId]);

  function startAdd() {
    setForm({ ...EMPTY });
    if (!services.length) api.get("/services/services/").then((r) => setServices(r.data.results || r.data)).catch(() => {});
    if (!materials.length)
      api
        .get("/warehouse/materials/", { params: { ordering: "name", page_size: 500 } })
        .then((r) => setMaterials(r.data.results || r.data))
        .catch(() => {});
  }

  async function save() {
    setBusy(true);
    try {
      const body = {
        client: clientId,
        price: form.price,
        note: form.note,
        ...(form.what === "material"
          ? { material: Number(form.material) || null, sale_mode: form.sale_mode }
          : { service: Number(form.service) || null, material: Number(form.material) || null }),
      };
      await api.post("/clients/client-prices/", body);
      setForm(null);
      load();
    } catch (e) {
      toast(apiError(e, t("common.error")), "error");
    } finally {
      setBusy(false);
    }
  }

  async function savePrice() {
    try {
      await api.patch(`/clients/client-prices/${editing.id}/`, {
        price: String(editing.price).replace(",", ".").replace(/\s/g, ""),
      });
      setEditing(null);
      load();
    } catch (e) {
      toast(apiError(e, t("common.error")), "error");
    }
  }

  async function remove(row) {
    if (!(await confirm(t("clientPrices.removeAsk", { what: label(row) })))) return;
    try {
      await api.delete(`/clients/client-prices/${row.id}/`);
      load();
    } catch (e) {
      toast(apiError(e, t("common.error")), "error");
    }
  }

  const label = (r) =>
    [r.service_name, r.material_name].filter(Boolean).join(" · ") +
    (r.sale_mode ? ` (${t(`clientPrices.mode_${r.sale_mode}`)})` : "");

  if (!rows.length && !canEdit) return null;

  return (
    <div className="field" style={{ marginTop: 14 }}>
      <label>{t("clientPrices.title")}</label>
      <p className="muted" style={{ fontSize: 12, margin: "0 0 6px" }}>{t("clientPrices.hint")}</p>
      {rows.map((r) => (
        <div className="crow" key={r.id} style={{ fontSize: 13 }}>
          <span>
            {label(r)}
            {r.note ? <span className="muted"> · {r.note}</span> : null}
          </span>
          <span style={{ display: "inline-flex", gap: 6, alignItems: "center" }}>
            {editing?.id === r.id ? (
              <>
                <input type="number" min="0" step="0.01" inputMode="decimal" style={{ width: 100 }}
                  aria-label={t("clientPrices.newPrice", { what: label(r) })}
                  value={editing.price} onChange={(e) => setEditing({ ...editing, price: e.target.value })} />
                <button type="button" className="row-btn" onClick={savePrice}>{t("common.save")}</button>
                <button type="button" className="ghost row-btn" onClick={() => setEditing(null)}>{t("common.cancel")}</button>
              </>
            ) : (
              <>
                <strong>{formatMoney(r.price, { fraction: 2 })}</strong>
                {canEdit && (
                  <>
                    <button type="button" className="ghost row-btn" onClick={() => setEditing({ id: r.id, price: String(r.price) })}>
                      {t("common.edit")}
                    </button>
                    <button type="button" className="ghost row-btn row-danger" onClick={() => remove(r)}>{t("common.delete")}</button>
                  </>
                )}
              </>
            )}
          </span>
        </div>
      ))}
      {canEdit && !form && (
        <button type="button" className="secondary" style={{ marginTop: 6 }} onClick={startAdd}>
          + {t("clientPrices.add")}
        </button>
      )}
      {form && (
        <div className="card" style={{ padding: 10, marginTop: 6 }}>
          <div className="row" style={{ gap: 6, flexWrap: "wrap", margin: 0, alignItems: "flex-end" }}>
            <select
              aria-label={t("clientPrices.what")}
              value={form.what}
              onChange={(e) => setForm({ ...form, what: e.target.value, service: "", material: "" })}
            >
              <option value="material">{t("clientPrices.whatMaterial")}</option>
              <option value="service">{t("clientPrices.whatService")}</option>
            </select>
            {form.what === "service" && (
              <select aria-label={t("clientPrices.service")} value={form.service}
                onChange={(e) => setForm({ ...form, service: e.target.value })}>
                <option value="">{t("clientPrices.service")}…</option>
                {services.map((s) => <option key={s.id} value={s.id}>{s.name}</option>)}
              </select>
            )}
            <select aria-label={t("clientPrices.material")} value={form.material}
              onChange={(e) => setForm({ ...form, material: e.target.value })}>
              <option value="">
                {form.what === "service" ? t("clientPrices.anyMaterial") : `${t("clientPrices.material")}…`}
              </option>
              {materials.map((m) => <option key={m.id} value={m.id}>{m.name}</option>)}
            </select>
            {form.what === "material" && (
              <select aria-label={t("clientPrices.mode")} value={form.sale_mode}
                onChange={(e) => setForm({ ...form, sale_mode: e.target.value })}>
                {["PIECE", "SQM", "METER"].map((m) => <option key={m} value={m}>{t(`clientPrices.mode_${m}`)}</option>)}
              </select>
            )}
            <input type="number" min="0" step="0.01" inputMode="decimal" style={{ width: 110 }}
              aria-label={t("clientPrices.price")} placeholder={t("clientPrices.price")}
              value={form.price} onChange={(e) => setForm({ ...form, price: e.target.value })} />
            <input style={{ flex: "1 1 140px" }} aria-label={t("clientPrices.note")} placeholder={t("clientPrices.note")}
              value={form.note} maxLength={255} onChange={(e) => setForm({ ...form, note: e.target.value })} />
          </div>
          <div className="row" style={{ gap: 6, margin: "8px 0 0" }}>
            <button type="button" onClick={save} disabled={busy || form.price === ""}>{t("common.save")}</button>
            <button type="button" className="secondary" onClick={() => setForm(null)}>{t("common.cancel")}</button>
          </div>
        </div>
      )}
    </div>
  );
}
