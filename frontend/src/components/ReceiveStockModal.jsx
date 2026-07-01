import { useMemo, useState } from "react";
import { useTranslation } from "react-i18next";

import api from "../api/api.js";
import Modal from "./Modal.jsx";
import { useUI } from "./UIProvider.jsx";

// Приём (поступление) нового прихода для КОНКРЕТНОГО материала — открывается с
// его строки в «Складе». Рулонный: сегмент Рулон/Лист + размеры + себестоимость
// партии (площадь и цена/кв.м считаются на лету). Штучный: количество (+ факт.
// закупочная цена). Дёргает те же эндпоинты, что раньше делал экран «Поступление».
export default function ReceiveStockModal({ material, onClose, onDone }) {
  const { t } = useTranslation();
  const { toast } = useUI();
  const roll = !!material.is_roll_material;
  const [form, setForm] = useState(roll ? "ROLL" : "QTY");
  const [v, setV] = useState({
    width: "", length: "", height: "", sheet_count: "",
    quantity: "", purchase_cost: "", actual_price: "", code: "",
  });
  const [busy, setBusy] = useState(false);
  const set = (k) => (e) => setV((s) => ({ ...s, [k]: e.target.value }));

  const area = useMemo(() => {
    const w = Number(v.width);
    if (form === "ROLL") return w && Number(v.length) ? w * Number(v.length) : 0;
    if (form === "SHEET")
      return w && Number(v.height) && Number(v.sheet_count) ? w * Number(v.height) * Number(v.sheet_count) : 0;
    return 0;
  }, [form, v.width, v.length, v.height, v.sheet_count]);

  const costPerSqm = area && Number(v.purchase_cost) ? (Number(v.purchase_cost) / area).toFixed(2) : null;
  const cur = Number(material.quantity) || 0;
  const unit = t(`unit.${material.unit}`);
  const added = roll ? area : Number(v.quantity) || 0;

  const valid = roll
    ? (form === "ROLL" ? v.width && v.length : v.width && v.height && v.sheet_count) && v.purchase_cost
    : !!v.quantity;

  async function submit() {
    setBusy(true);
    try {
      if (roll) {
        await api.post("/warehouse/materials/receive-roll/", {
          material: material.id,
          form,
          code: v.code,
          width: Number(v.width),
          length: form === "ROLL" ? Number(v.length) : null,
          height: form === "SHEET" ? Number(v.height) : null,
          sheet_count: form === "SHEET" ? Number(v.sheet_count) : null,
          purchase_cost: Number(v.purchase_cost),
        });
      } else {
        await api.post("/warehouse/materials/supply/", {
          material: material.id,
          quantity: Number(v.quantity),
          actual_price: v.actual_price ? Number(v.actual_price) : null,
          reason: v.code,
        });
      }
      toast(t("supply.done"));
      onDone?.();
      onClose();
    } catch (e) {
      const data = e.response?.data;
      const first = data && (data.detail || (typeof data === "object" ? Object.values(data)[0] : data));
      toast(Array.isArray(first) ? first[0] : first || t("common.error"), "error");
    } finally {
      setBusy(false);
    }
  }

  const numField = (label, key, extra) => (
    <div className="field grow" style={{ margin: 0 }}>
      <label>{label}</label>
      <input type="number" step="any" value={v[key]} onChange={set(key)} {...extra} />
    </div>
  );

  return (
    <Modal
      title={`${t("supply.intake")}: ${material.name}`}
      onClose={onClose}
      footer={
        <>
          <button className="secondary" onClick={onClose}>{t("common.cancel")}</button>
          <button onClick={submit} disabled={busy || !valid}>{t("supply.intake")}</button>
        </>
      }
    >
      {roll && (
        <div className="tabs" style={{ marginTop: 0 }}>
          {[["ROLL", t("supply.formRoll")], ["SHEET", t("supply.formSheet")]].map(([k, label]) => (
            <button key={k} className={form === k ? "active" : ""} onClick={() => setForm(k)}>
              {label}
            </button>
          ))}
        </div>
      )}

      {roll && form === "ROLL" && (
        <div className="row">
          {numField(t("supply.width"), "width", { autoFocus: true })}
          {numField(t("supply.length"), "length")}
        </div>
      )}
      {roll && form === "SHEET" && (
        <div className="row">
          {numField(t("supply.width"), "width", { autoFocus: true })}
          {numField(t("supply.height"), "height")}
          {numField(t("supply.sheets"), "sheet_count")}
        </div>
      )}
      {roll && (
        <div className="field" style={{ marginTop: 12 }}>
          <label>{t("supply.batchCost")}</label>
          <input type="number" step="any" value={v.purchase_cost} onChange={set("purchase_cost")} />
        </div>
      )}

      {!roll && (
        <div className="row">
          {numField(`${t("common.quantity")} (${unit})`, "quantity", { autoFocus: true })}
          {numField(t("supply.actualPrice"), "actual_price")}
        </div>
      )}

      <div className="field">
        <label>{t("supply.rollCode")}</label>
        <input value={v.code} onChange={set("code")} placeholder={t("supply.batchPlaceholder")} />
      </div>

      {added > 0 && (
        <div className="card" style={{ background: "var(--canvas)", padding: 12 }}>
          {roll && (
            <>
              <div className="crow"><span className="k">{t("supply.area")}</span><strong>{area.toFixed(2)} кв.м</strong></div>
              {costPerSqm && (
                <div className="crow"><span className="k">{t("supply.costPerSqm")}</span><strong>{costPerSqm} сом/кв.м</strong></div>
              )}
            </>
          )}
          <div className="crow">
            <span className="k">{t("supply.becomes")}</span>
            <strong>{cur} → {(cur + added).toFixed(2)} {roll ? "кв.м" : unit}</strong>
          </div>
        </div>
      )}
    </Modal>
  );
}
