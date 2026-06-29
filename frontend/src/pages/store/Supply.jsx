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
const EMPTY_INTAKE = {
  material: "",
  form: "ROLL",
  code: "",
  width: "",
  length: "",
  height: "",
  sheet_count: "",
  quantity: "",
  purchase_cost: "",
  markup_percent: "20",
  actual_price: "",
  reason: "",
};

export default function Supply({ embedded = false }) {
  const { t } = useTranslation();
  const { toast } = useUI();
  const [tab, setTab] = useState("intake");
  const [materials, setMaterials] = useState([]);
  const [busy, setBusy] = useState(false);

  const [intake, setIntake] = useState(EMPTY_INTAKE);
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

  // --- intake area / price preview ---
  const intakeArea = () => {
    const w = Number(intake.width);
    if (intake.form === "ROLL") return w && Number(intake.length) ? w * Number(intake.length) : 0;
    if (intake.form === "SHEET")
      return w && Number(intake.height) && Number(intake.sheet_count)
        ? w * Number(intake.height) * Number(intake.sheet_count)
        : 0;
    return 0;
  };
  const intakeCostPerSqm = () => {
    const a = intakeArea();
    const c = Number(intake.purchase_cost);
    if (!a || !c) return null;
    return (c / a).toFixed(2);
  };

  const submitIntake = () =>
    run(async () => {
      if (intake.form === "QTY") {
        await api.post("/warehouse/materials/supply/", {
          material: Number(intake.material),
          quantity: Number(intake.quantity),
          actual_price: intake.actual_price ? Number(intake.actual_price) : null,
          reason: intake.reason,
        });
      } else {
        await api.post("/warehouse/materials/receive-roll/", {
          material: Number(intake.material),
          form: intake.form,
          code: intake.code,
          width: Number(intake.width),
          length: intake.form === "ROLL" ? Number(intake.length) : null,
          height: intake.form === "SHEET" ? Number(intake.height) : null,
          sheet_count: intake.form === "SHEET" ? Number(intake.sheet_count) : null,
          purchase_cost: Number(intake.purchase_cost),
        });
      }
      setIntake(EMPTY_INTAKE);
    });

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

  const intakeValid =
    intake.material &&
    intake.purchase_cost &&
    (intake.form === "QTY"
      ? intake.quantity
      : intake.form === "ROLL"
      ? intake.width && intake.length
      : intake.width && intake.height && intake.sheet_count);

  const TABS = [
    ["intake", t("supply.intake")],
    ["inventory", t("supply.inventory")],
    ["writeoff", t("supply.writeoff")],
  ];
  const FORMS = [
    ["ROLL", t("supply.formRoll")],
    ["SHEET", t("supply.formSheet")],
    ["QTY", t("supply.formQty")],
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

      {tab === "intake" && (
        <div className="card" style={{ maxWidth: 560 }}>
          <Field label={t("checkout.material")}>
            <MaterialSelect value={intake.material} onChange={(e) => setIntake({ ...intake, material: e.target.value })} materials={materials} t={t} />
          </Field>

          <Field label={t("supply.form")}>
            <div className="tabs" style={{ margin: 0 }}>
              {FORMS.map(([key, label]) => (
                <button
                  key={key}
                  className={intake.form === key ? "active" : ""}
                  onClick={() => setIntake({ ...intake, form: key })}
                >
                  {label}
                </button>
              ))}
            </div>
          </Field>

          {intake.form === "ROLL" && (
            <div className="row">
              <Field label={t("supply.width")}><Num value={intake.width} onChange={(v) => setIntake({ ...intake, width: v })} /></Field>
              <Field label={t("supply.length")}><Num value={intake.length} onChange={(v) => setIntake({ ...intake, length: v })} /></Field>
            </div>
          )}
          {intake.form === "SHEET" && (
            <div className="row">
              <Field label={t("supply.width")}><Num value={intake.width} onChange={(v) => setIntake({ ...intake, width: v })} /></Field>
              <Field label={t("supply.height")}><Num value={intake.height} onChange={(v) => setIntake({ ...intake, height: v })} /></Field>
              <Field label={t("supply.sheets")}><Num value={intake.sheet_count} onChange={(v) => setIntake({ ...intake, sheet_count: v })} /></Field>
            </div>
          )}
          {intake.form === "QTY" && (
            <Field label={t("common.quantity")}>
              <Num value={intake.quantity} onChange={(v) => setIntake({ ...intake, quantity: v })} />
            </Field>
          )}

          {intake.form !== "QTY" && (
            <Field label={t("supply.batchCost")}>
              <Num value={intake.purchase_cost} onChange={(v) => setIntake({ ...intake, purchase_cost: v })} />
            </Field>
          )}
          {intake.form === "QTY" && (
            <Field label={t("supply.actualPrice")}>
              <Num value={intake.actual_price} onChange={(v) => setIntake({ ...intake, actual_price: v })} />
            </Field>
          )}

          <Field label={t("supply.rollCode")}>
            <input value={intake.code} onChange={(e) => setIntake({ ...intake, code: e.target.value })} placeholder="Партия / поставщик" />
          </Field>

          {/* live preview */}
          {intake.form !== "QTY" && intakeArea() > 0 && (
            <div className="card" style={{ background: "var(--canvas)", padding: 12, marginBottom: 14 }}>
              <div className="crow"><span className="k">{t("supply.area")}</span><strong>{intakeArea().toFixed(2)} кв.м</strong></div>
              {intakeCostPerSqm() && (
                <div className="crow"><span className="k">{t("supply.costPerSqm")}</span><strong>{intakeCostPerSqm()} сом/кв.м</strong></div>
              )}
              {intake.material && (
                <div className="crow"><span className="k">{t("supply.becomes")}</span><strong>{stockOf(intake.material)} → {(Number(stockOf(intake.material)) + intakeArea()).toFixed(2)} кв.м</strong></div>
              )}
            </div>
          )}
          {intake.form === "QTY" && intake.material && intake.quantity && (
            <div className="card" style={{ background: "var(--canvas)", padding: 12, marginBottom: 14 }}>
              <div className="crow"><span className="k">{t("supply.becomes")}</span><strong>{stockOf(intake.material)} → {(Number(stockOf(intake.material)) + Number(intake.quantity)).toFixed(2)}</strong></div>
            </div>
          )}

          <button style={{ width: "100%", height: 50 }} onClick={submitIntake} disabled={busy || !intakeValid}>
            {t("supply.intake")}
          </button>
        </div>
      )}

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
