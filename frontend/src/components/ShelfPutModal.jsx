/**
 * «Положить на полку» / «Остатки на полку» из карточки заказа (D-200).
 *
 * Куски после заказа уже списаны заказом: склад и его стоимость не меняются,
 * это только учёт того, что лежит. Из карточки заказа материал по умолчанию —
 * из строк заказа (сервер отдаёт их в `from-receipt`). Несколько кусков разного
 * размера — несколько строк, уходят одним запросом.
 */
import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";

import api from "../api/api.js";
import { apiError } from "../api/errors.js";
import { parseNumber } from "../utils/pasteTable.js";
import Field from "./Field.jsx";
import Modal from "./Modal.jsx";
import { useUI } from "./UIProvider.jsx";

const MEASURES = ["SQM", "METER", "PIECE"];

// Мерка по материалу: рулон — метры, лист — квадраты, штучное — штуки.
const measureOf = (m) => (!m ? "SQM" : m.measure || (m.sells_by_metre ? "METER" : m.is_roll_material ? "SQM" : "PIECE"));

const blankRow = (material) => ({
  material: material ? String(material.id) : "",
  measure: measureOf(material),
  width: "",
  length: "",
  pieces: "1",
  note: "",
});

/** Чего не хватает строке — или null, если её можно класть. */
function rowProblem(row) {
  if (!row.material) return "needMaterial";
  const pieces = Number(row.pieces);
  if (!(pieces >= 1) || !Number.isInteger(pieces)) return "needPieces";
  const w = parseNumber(row.width);
  const l = parseNumber(row.length);
  if (row.measure === "SQM" && !(w > 0 && l > 0)) return "needSize";
  if (row.measure === "METER" && !(l > 0)) return "needLength";
  return null;
}

export default function ShelfPutModal({ receipt = null, onClose, onDone }) {
  const { t } = useTranslation();
  const { toast } = useUI();
  const [materials, setMaterials] = useState([]);
  const [orderMaterials, setOrderMaterials] = useState([]);
  const [already, setAlready] = useState([]);
  const [sites, setSites] = useState([]);
  const [site, setSite] = useState("");
  const [rows, setRows] = useState([blankRow(null)]);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    api
      .get("/warehouse/materials/", { params: { ordering: "name", page_size: 500 } })
      .then((r) => setMaterials(r.data.results ?? r.data))
      .catch(() => setMaterials([]));
    api
      .get("/warehouse/production-sites/")
      .then((r) => setSites(r.data.results ?? r.data))
      .catch(() => setSites([]));
  }, []);

  useEffect(() => {
    if (!receipt) return;
    api
      .get("/warehouse/leftovers/from-receipt/", { params: { receipt: receipt.id } })
      .then((r) => {
        setOrderMaterials(r.data.materials || []);
        setAlready(r.data.leftovers || []);
        // Материал по умолчанию — первый из строк заказа.
        if (r.data.materials?.length) setRows([blankRow(r.data.materials[0])]);
      })
      .catch(() => {});
  }, [receipt]);

  const known = new Set(orderMaterials.map((m) => m.id));
  const byId = (id) => orderMaterials.find((m) => String(m.id) === String(id))
    || materials.find((m) => String(m.id) === String(id));

  const update = (i, patch) => setRows((list) => list.map((r, j) => (j === i ? { ...r, ...patch } : r)));
  const problems = rows.map(rowProblem);
  const ready = rows.length > 0 && problems.every((p) => p === null);

  async function submit() {
    setBusy(true);
    try {
      const items = rows.map((r) => ({
        material: Number(r.material),
        measure: r.measure,
        pieces: Number(r.pieces),
        width: r.measure === "METER" ? null : parseNumber(r.width) || null,
        length: parseNumber(r.length) || null,
        note: r.note.trim(),
        site: site ? Number(site) : null,
        ...(receipt ? { source_receipt: receipt.id } : {}),
      }));
      const { data } = await api.post("/warehouse/leftovers/", { items });
      toast(t("shelf.putDone", { n: data.results?.length ?? items.length }));
      onDone?.(data.results);
    } catch (e) {
      toast(apiError(e, t("common.error")), "error");
    } finally {
      setBusy(false);
    }
  }

  return (
    <Modal
      title={receipt ? t("shelf.putTitleOrder", { n: receipt.order_number }) : t("shelf.putTitle")}
      onClose={onClose}
      footer={
        <>
          <button className="secondary" onClick={onClose}>{t("common.cancel")}</button>
          <button onClick={submit} disabled={busy || !ready}>{t("shelf.put")}</button>
        </>
      }
    >
      <p className="muted" style={{ fontSize: 13, marginTop: 0 }}>{t("shelf.putHint")}</p>
      {already.length > 0 && (
        <p className="muted" style={{ fontSize: 13 }}>
          {t("shelf.alreadyPut")} {already.map((lo) => `${lo.label} × ${lo.pieces}`).join(" · ")}
        </p>
      )}
      {rows.map((row, i) => (
        <div className="shelf-put-row" key={i}>
          <Field label={t("shelf.colMaterial")} required>
            <select
              value={row.material}
              onChange={(e) => update(i, { material: e.target.value, measure: measureOf(byId(e.target.value)) })}
            >
              <option value="">{t("shelf.chooseMaterial")}</option>
              {orderMaterials.length > 0 && (
                <optgroup label={t("shelf.orderMaterials")}>
                  {orderMaterials.map((m) => <option key={`o${m.id}`} value={m.id}>{m.name}</option>)}
                </optgroup>
              )}
              <optgroup label={orderMaterials.length ? t("shelf.otherMaterials") : t("shelf.colMaterial")}>
                {materials.filter((m) => !known.has(m.id)).map((m) => (
                  <option key={m.id} value={m.id}>{m.name}</option>
                ))}
              </optgroup>
            </select>
          </Field>
          <div className="field">
            <label>{t("shelf.measure")}</label>
            <div className="tabs" style={{ marginTop: 0 }} role="group" aria-label={t("shelf.measure")}>
              {MEASURES.map((m) => (
                <button
                  key={m}
                  type="button"
                  className={row.measure === m ? "active" : ""}
                  aria-pressed={row.measure === m}
                  onClick={() => update(i, { measure: m, width: m === "METER" ? "" : row.width })}
                >
                  {t(`shelf.measure${m}`)}
                </button>
              ))}
            </div>
          </div>
          <div className="row">
            {row.measure !== "METER" && (
              <Field className="grow" label={t("shelf.width")} required={row.measure === "SQM"}>
                <input inputMode="decimal" value={row.width} onChange={(e) => update(i, { width: e.target.value })} />
              </Field>
            )}
            <Field className="grow" label={t("shelf.length")} required={row.measure !== "PIECE"}>
              <input inputMode="decimal" value={row.length} onChange={(e) => update(i, { length: e.target.value })} />
            </Field>
            <Field className="grow" label={t("shelf.pieces")} required>
              <input type="number" inputMode="numeric" min="1" step="1" value={row.pieces}
                     onChange={(e) => update(i, { pieces: e.target.value })} />
            </Field>
          </div>
          <Field label={t("shelf.note")}>
            <input value={row.note} placeholder={t("shelf.notePh")} onChange={(e) => update(i, { note: e.target.value })} />
          </Field>
          {problems[i] && <p className="disabled-reason" role="status">{t(`shelf.block.${problems[i]}`)}</p>}
          {rows.length > 1 && (
            <button type="button" className="ghost row-btn row-danger"
                    onClick={() => setRows((list) => list.filter((_, j) => j !== i))}>
              {t("shelf.removeRow")}
            </button>
          )}
        </div>
      ))}
      <button
        type="button"
        className="secondary row-btn"
        onClick={() => setRows((list) => [...list, { ...blankRow(byId(list[list.length - 1]?.material)), material: list[list.length - 1]?.material || "" }])}
      >
        {t("shelf.addRow")}
      </button>
      {sites.length > 0 && (
        <Field label={t("shelf.site")} style={{ marginTop: 12 }}>
          <select value={site} onChange={(e) => setSite(e.target.value)}>
            <option value="">{t("shelf.noSite")}</option>
            {sites.map((s) => <option key={s.id} value={s.id}>{s.name}</option>)}
          </select>
        </Field>
      )}
    </Modal>
  );
}
