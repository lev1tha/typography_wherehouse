import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";

import api from "../../api/api.js";
import { useUI } from "../../components/UIProvider.jsx";

// Module-level so they keep a stable identity across renders — defining these
// inside the component remounts the inputs on every keystroke (focus loss).
const Field = ({ label, children }) => (
  <div className="field">
    <label>{label}</label>
    {children}
  </div>
);
const Num = ({ value, onChange }) => (
  <input type="number" step="any" value={value} onChange={(e) => onChange(e.target.value)} />
);
const MaterialSelect = ({ value, onChange, materials, t }) => (
  <select value={value} onChange={onChange}>
    <option value="">—</option>
    {materials.map((m) => (
      <option key={m.id} value={m.id}>
        {m.name} ({t("supply.currentStock")}: {m.quantity} {t(`unit.${m.unit}`)})
      </option>
    ))}
  </select>
);

const WRITEOFF_REASONS = ["DAMAGE", "DEFECT", "LOSS", "EXPIRY", "OTHER"];

// «Движение»: инвентаризация (пересчёт) и списание (порча/брак/утеря). Приём
// нового прихода вынесен на строку материала в «Материалах» (ReceiveStockModal).
export default function Supply({ embedded = false }) {
  const { t } = useTranslation();
  const { toast } = useUI();
  const [tab, setTab] = useState("inventory");
  const [materials, setMaterials] = useState([]);
  const [busy, setBusy] = useState(false);

  const [inv, setInv] = useState({ material: "", counted_quantity: "", reason: "" });
  const [writeoff, setWriteoff] = useState({ material: "", quantity: "", reason_code: "DAMAGE", note: "" });

  function load() {
    api.get("/warehouse/materials/", { params: { ordering: "name" } }).then((r) => setMaterials(r.data.results));
  }
  useEffect(load, []);

  const stockOf = (id) => {
    const m = materials.find((x) => x.id === Number(id));
    return m ? Number(m.quantity) : null;
  };

  async function run(fn) {
    setBusy(true);
    try {
      await fn();
      toast(t("supply.done"));
      load();
    } catch (e) {
      const data = e.response?.data;
      const first = data && (data.detail || (typeof data === "object" ? Object.values(data)[0] : data));
      toast(Array.isArray(first) ? first[0] : first || t("common.error"), "error");
    } finally {
      setBusy(false);
    }
  }

  const submitInventory = () =>
    run(async () => {
      await api.post("/warehouse/materials/adjust/", {
        material: Number(inv.material),
        counted_quantity: Number(inv.counted_quantity),
        reason: inv.reason,
      });
      setInv({ material: "", counted_quantity: "", reason: "" });
    });

  const submitWriteOff = () =>
    run(async () => {
      await api.post("/warehouse/materials/write-off/", {
        material: Number(writeoff.material),
        quantity: Number(writeoff.quantity),
        reason_code: writeoff.reason_code,
        note: writeoff.note,
      });
      setWriteoff({ material: "", quantity: "", reason_code: "DAMAGE", note: "" });
    });

  const TABS = [
    ["inventory", t("supply.inventory")],
    ["writeoff", t("supply.writeoff")],
  ];

  return (
    <>
      {!embedded && <h1>{t("supply.title")}</h1>}
      <div className="tabs">
        {TABS.map(([key, label]) => (
          <button key={key} className={tab === key ? "active" : ""} onClick={() => setTab(key)}>
            {label}
          </button>
        ))}
      </div>

      {tab === "inventory" && (
        <div className="card" style={{ maxWidth: 520 }}>
          <Field label={t("checkout.material")}>
            <MaterialSelect value={inv.material} onChange={(e) => setInv({ ...inv, material: e.target.value })} materials={materials} t={t} />
          </Field>
          <Field label={t("supply.counted")}>
            <Num value={inv.counted_quantity} onChange={(v) => setInv({ ...inv, counted_quantity: v })} />
          </Field>
          {inv.material && inv.counted_quantity !== "" && (
            <div className="card" style={{ background: "var(--canvas)", padding: 12, marginBottom: 14 }}>
              <div className="crow"><span className="k">{t("supply.becomes")}</span><strong>{stockOf(inv.material)} → {Number(inv.counted_quantity)}</strong></div>
            </div>
          )}
          <Field label={t("supply.reason")}>
            <input value={inv.reason} onChange={(e) => setInv({ ...inv, reason: e.target.value })} />
          </Field>
          <button style={{ width: "100%", height: 50 }} onClick={submitInventory} disabled={busy || !inv.material || inv.counted_quantity === ""}>
            {t("supply.inventory")}
          </button>
        </div>
      )}

      {tab === "writeoff" && (
        <div className="card" style={{ maxWidth: 520 }}>
          <Field label={t("checkout.material")}>
            <MaterialSelect value={writeoff.material} onChange={(e) => setWriteoff({ ...writeoff, material: e.target.value })} materials={materials} t={t} />
          </Field>
          <Field label={t("supply.writeoffQty")}>
            <Num value={writeoff.quantity} onChange={(v) => setWriteoff({ ...writeoff, quantity: v })} />
          </Field>
          {writeoff.material && writeoff.quantity && (
            <div className="card" style={{ background: "var(--canvas)", padding: 12, marginBottom: 14 }}>
              <div className="crow"><span className="k">{t("supply.becomes")}</span><strong>{stockOf(writeoff.material)} → {(Number(stockOf(writeoff.material)) - Number(writeoff.quantity)).toFixed(2)}</strong></div>
            </div>
          )}
          <Field label={t("supply.writeoffReason")}>
            <select value={writeoff.reason_code} onChange={(e) => setWriteoff({ ...writeoff, reason_code: e.target.value })}>
              {WRITEOFF_REASONS.map((code) => (
                <option key={code} value={code}>{t(`writeoffReason.${code}`)}</option>
              ))}
            </select>
          </Field>
          <Field label={t("supply.note")}>
            <input value={writeoff.note} onChange={(e) => setWriteoff({ ...writeoff, note: e.target.value })} />
          </Field>
          <button className="danger" style={{ width: "100%", height: 50 }} onClick={submitWriteOff} disabled={busy || !writeoff.material || !writeoff.quantity}>
            {t("supply.writeoff")}
          </button>
        </div>
      )}
    </>
  );
}
