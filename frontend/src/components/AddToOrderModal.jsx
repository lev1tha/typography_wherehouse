import { useEffect, useMemo, useState } from "react";
import { useTranslation } from "react-i18next";

import api from "../api/api.js";
import Modal from "./Modal.jsx";
import { useUI } from "./UIProvider.jsx";

/** Configure and append one item (дозаказ) to an existing receipt. */
export default function AddToOrderModal({ receiptId, onClose, onAdded }) {
  const { t } = useTranslation();
  const { toast } = useUI();
  const [services, setServices] = useState([]);
  const [materials, setMaterials] = useState([]);
  const [pick, setPick] = useState(""); // "S<id>" | "M<id>"
  const [cfg, setCfg] = useState({ qty: "1", width: "", length: "", letter_type: "FLAT", materialId: "" });
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    api.get("/services/services/").then((r) => setServices(r.data.results.filter((s) => s.is_active !== false)));
    api.get("/warehouse/materials/", { params: { ordering: "name" } }).then((r) => setMaterials(r.data.results));
  }, []);

  const areaMaterials = materials.filter((m) => m.is_roll_material);
  const sel = useMemo(() => {
    if (!pick) return null;
    if (pick[0] === "S") return { type: "service", obj: services.find((s) => s.id === Number(pick.slice(1))) };
    return { type: "material", obj: materials.find((m) => m.id === Number(pick.slice(1))) };
  }, [pick, services, materials]);

  const svc = sel?.type === "service" ? sel.obj : null;
  const usesArea = svc?.uses_area;
  const cfgMat = materials.find((m) => m.id === Number(cfg.materialId));

  // Live price preview.
  let preview = 0;
  if (sel?.type === "material") preview = Number(sel.obj.price_per_unit) * Number(cfg.qty || 0);
  else if (svc?.uses_area) {
    const area = Number(cfg.width) * Number(cfg.length) || 0;
    const rate = svc.uses_letter_type && cfg.letter_type === "VOLUMETRIC" ? Number(svc.rate_volumetric) : Number(svc.rate_flat);
    preview = (rate + (cfgMat ? Number(cfgMat.price_per_unit) : 0)) * area;
  } else if (svc?.uses_pieces) preview = Number(svc.rate_per_piece) * Number(cfg.qty || 0);
  else if (svc) preview = Number(svc.base_price) * Number(cfg.qty || 0);

  function buildItem() {
    if (sel.type === "material") return { type: "MATERIAL", material: sel.obj.id, quantity: Number(cfg.qty) };
    if (svc.uses_area) {
      const it = { type: "SERVICE", service: svc.id, material: Number(cfg.materialId), width: Number(cfg.width), length: Number(cfg.length) };
      if (svc.uses_letter_type) it.letter_type = cfg.letter_type;
      return it;
    }
    return { type: "SERVICE", service: svc.id, quantity: Number(cfg.qty) };
  }

  const valid =
    sel &&
    (sel.type === "material"
      ? Number(cfg.qty) > 0
      : svc.uses_area
      ? cfg.materialId && Number(cfg.width) > 0 && Number(cfg.length) > 0
      : Number(cfg.qty) > 0);

  async function add() {
    setBusy(true);
    try {
      const { data } = await api.post(`/sales/receipts/${receiptId}/add-items/`, { items: [buildItem()] });
      toast(t("receipts.added"));
      onAdded(data);
    } catch (e) {
      toast(e.response?.data?.detail || t("common.error"), "error");
    } finally {
      setBusy(false);
    }
  }

  return (
    <Modal
      title={t("receipts.addToOrder")}
      onClose={onClose}
      footer={
        <>
          <button className="secondary" onClick={onClose}>{t("common.cancel")}</button>
          <button onClick={add} disabled={busy || !valid}>{t("common.add")}</button>
        </>
      }
    >
      <div className="field">
        <label>{t("checkout.addItem")}</label>
        <select value={pick} onChange={(e) => { setPick(e.target.value); setCfg({ qty: "1", width: "", length: "", letter_type: "FLAT", materialId: areaMaterials[0]?.id ? String(areaMaterials[0].id) : "" }); }}>
          <option value="">—</option>
          <optgroup label={t("checkout.service")}>
            {services.filter((s) => s.is_active !== false).map((s) => (
              <option key={s.id} value={`S${s.id}`}>{s.name}</option>
            ))}
          </optgroup>
          <optgroup label={t("checkout.material")}>
            {materials.map((m) => (
              <option key={m.id} value={`M${m.id}`}>{m.name}</option>
            ))}
          </optgroup>
        </select>
      </div>

      {svc?.uses_letter_type && (
        <div className="field">
          <label>{t("checkout.letterTypeLabel")}</label>
          <div className="tabs" style={{ margin: 0 }}>
            {["FLAT", "VOLUMETRIC"].map((lt) => (
              <button key={lt} className={cfg.letter_type === lt ? "active" : ""} onClick={() => setCfg({ ...cfg, letter_type: lt })}>
                {t(`letterType.${lt}`)}
              </button>
            ))}
          </div>
        </div>
      )}

      {usesArea && (
        <>
          <div className="field">
            <label>{t("checkout.cutMaterial")}</label>
            <select value={cfg.materialId} onChange={(e) => setCfg({ ...cfg, materialId: e.target.value })}>
              <option value="">—</option>
              {areaMaterials.map((m) => (
                <option key={m.id} value={m.id}>{m.name} ({m.price_per_unit} сом/кв.м, ост. {m.quantity})</option>
              ))}
            </select>
          </div>
          <div className="row">
            <div className="field grow"><label>{t("supply.width")}</label><input type="number" value={cfg.width} onChange={(e) => setCfg({ ...cfg, width: e.target.value })} /></div>
            <div className="field grow"><label>{t("supply.length")}</label><input type="number" value={cfg.length} onChange={(e) => setCfg({ ...cfg, length: e.target.value })} /></div>
          </div>
        </>
      )}

      {sel && !usesArea && (
        <div className="field">
          <label>{sel.type === "service" && svc.uses_pieces ? t("receipts.letters") : t("common.quantity")}</label>
          <input type="number" value={cfg.qty} onChange={(e) => setCfg({ ...cfg, qty: e.target.value })} />
        </div>
      )}

      {sel && preview > 0 && (
        <div className="card" style={{ background: "var(--canvas)", padding: 12 }}>
          <div className="crow"><span className="k">{t("checkout.submit")}</span><strong style={{ fontSize: 18 }}>+{preview.toFixed(0)} сом</strong></div>
        </div>
      )}
    </Modal>
  );
}
